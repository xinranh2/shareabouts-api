import ujson as json
from django.contrib.gis.db import models
from django.contrib.gis.db.models import query
from django.conf import settings
from django.core.files.storage import get_storage_class
from django.db.models.signals import post_save
from django.utils.timezone import now
from .. import cache
from .. import utils
from .caching import CacheClearingModel
from .data_indexes import IndexedValue, FilterByIndexMixin
from .profiles import User


class CloneableModelMixin (object):
    """
    Mixin providing a clone method that copies all of a models instance's
    fields to a new instance of the model, allowing overrides.

    """
    def get_ignore_fields(self, ModelClass):
        fields = ModelClass._meta.fields
        pk_name = ModelClass._meta.pk.name

        ignore_field_names = set([pk_name])
        for fld in fields:
            if fld.name == pk_name:
                pk_fld = fld
                break
        else:
            raise Exception('Model %s somehow has no PK field' % (ModelClass,))

        if pk_fld.rel and pk_fld.rel.parent_link:
            parent_ignore_fields = self.get_ignore_fields(pk_fld.rel.to)
            ignore_field_names.update(parent_ignore_fields)

        return ignore_field_names

    def clone(self, inst_kwargs=None, commit=True):
        """
        Create a duplicate of the model instance, replacing any properties
        specified as keyword arguments. This is a simple base implementation
        and may need to be extended for specific classes, since it is
        does not address related fields in any way.
        """
        fields = self._meta.fields
        inst_kwargs = inst_kwargs or {}
        ignore_field_names = self.get_ignore_fields(self.__class__)

        for fld in fields:
            if fld.name not in ignore_field_names:
                fld_value = getattr(self, fld.name)
                inst_kwargs.setdefault(fld.name, fld_value)

        new_inst = self.__class__(**inst_kwargs)

        if commit:
            new_inst.save()

        return new_inst


class TimeStampedModel (models.Model):
    created_datetime = models.DateTimeField(default=now, blank=True, db_index=True)
    updated_datetime = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class ModelWithDataBlob (models.Model):
    data = models.TextField(default='{}')

    class Meta:
        abstract = True


class SubmittedThingQuerySet (FilterByIndexMixin, query.QuerySet):
    pass


class SubmittedThingManager (FilterByIndexMixin, models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return SubmittedThingQuerySet(self.model, using=self._db)


class SubmittedThing (CacheClearingModel, ModelWithDataBlob, TimeStampedModel):
    """
    A SubmittedThing generally comes from the end-user.  It may be a place, a
    comment, a vote, etc.

    """
    submitter = models.ForeignKey(User, related_name='things', null=True, blank=True)
    dataset = models.ForeignKey('DataSet', related_name='things', blank=True)
    visible = models.BooleanField(default=True, blank=True, db_index=True)

    objects = SubmittedThingManager()

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_submittedthing'

    def index_values(self, indexes=None):
        if indexes is None:
            indexes = self.dataset.indexes.all()

        if len(indexes) == 0:
            return

        data = json.loads(self.data)
        for index in indexes:
            IndexedValue.objects.sync(self, index, data=data)

    def save(self, silent=False, source='', reindex=True, *args, **kwargs):
        is_new = (self.id == None)

        ret = super(SubmittedThing, self).save(*args, **kwargs)

        if reindex:
            self.index_values()

        # All submitted things generate an action if not silent.
        if not silent:
            action = Action()
            action.action = 'create' if is_new else 'update'
            action.thing = self
            action.source = source
            action.save()

        return ret


class DataSet (CacheClearingModel, models.Model):
    """
    A DataSet is a named collection of data, eg. Places, owned by a user,
    and intended for a coherent purpose, eg. display on a single map.
    """
    owner = models.ForeignKey(User, related_name='datasets')
    display_name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=128, default=u'')

    cache = cache.DataSetCache()
    # previous_version = 'sa_api_v1.models.DataSet'

    def __unicode__(self):
        return self.slug

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_dataset'
        unique_together = (('owner', 'slug'),
                           )

    @property
    def places(self):
        if not hasattr(self, '_places'):
            self._places = Place.objects.filter(dataset=self)
        return self._places

    @property
    def submissions(self):
        if not hasattr(self, '_submissions'):
            self._submissions = Submission.objects.filter(dataset=self)
        return self._submissions

    @utils.memo
    def get_key(self, key_string):
        for ds_key in self.keys.all():
            if ds_key.key == key_string:
                return ds_key
        return None

    @utils.memo
    def get_origin(self, origin_header):
        for ds_origin in self.origins.all():
            if ds_origin.match(ds_origin.pattern, origin_header):
                return ds_origin
        return None

    def reindex(self):
        things = self.things.all()
        indexes = self.indexes.all()

        for thing in things:
            thing.index_values(indexes)


def after_create_dataset(sender, instance, created, **kwargs):
    """
    Add planbox as an allowed origin on all datasets.
    """
    if created:
        from sa_api_v2.cors.models import Origin
        # openplans.org domains
        origin = Origin.objects.create(dataset=instance, pattern='(?:www.)?openplans.org')
        origin.permissions.all().update(can_update=False, can_destroy=False)
        # openplans github domain
        origin = Origin.objects.create(dataset=instance, pattern='openplans.github.io')
        origin.permissions.all().update(can_update=False, can_destroy=False)
post_save.connect(after_create_dataset, sender=DataSet, dispatch_uid="dataset-create")


class Webhook (TimeStampedModel):
    """
    A Webhook is a user-defined HTTP callback for POSTing place or submitted
    thing as JSON to a specified URL after a specified event.

    """
    EVENT_CHOICES = (
        ('add', 'On add'),
    )

    dataset = models.ForeignKey('DataSet', related_name='webhooks')
    submission_set = models.CharField(max_length=128)
    event = models.CharField(max_length=128, choices=EVENT_CHOICES, default='add')
    url = models.URLField(max_length=2048)

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_webhook'

    def __unicode__(self):
        return 'On %s data in %s' % (self.event, self.submission_set)


class GeoSubmittedThingQuerySet (query.GeoQuerySet, SubmittedThingQuerySet):
    pass


class GeoSubmittedThingManager (models.GeoManager, SubmittedThingManager):
    def get_queryset(self):
        return GeoSubmittedThingQuerySet(self.model, using=self._db)


class Place (SubmittedThing):
    """
    A Place is a submitted thing with some geographic information, to which
    other submissions such as comments or surveys can be attached.

    """
    geometry = models.GeometryField()

    objects = GeoSubmittedThingManager()
    cache = cache.PlaceCache()
    # previous_version = 'sa_api_v1.models.Place'

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_place'
        ordering = ['-updated_datetime']


class Submission (CloneableModelMixin, SubmittedThing):
    """
    A Submission is the simplest flavor of SubmittedThing.
    It belongs to a Place.
    Used for representing eg. comments, votes, ...
    """
    place = models.ForeignKey(Place, related_name='submissions')
    set_name = models.TextField(db_index=True)

    objects = SubmittedThingManager()
    cache = cache.SubmissionCache()
    # previous_version = 'sa_api_v1.models.Submission'

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_submission'
        ordering = ['-updated_datetime']


class Action (CacheClearingModel, TimeStampedModel):
    """
    Metadata about SubmittedThings:
    what happened when.
    """
    action = models.CharField(max_length=16, default='create')
    thing = models.ForeignKey(SubmittedThing, db_column='data_id', related_name='actions')
    source = models.TextField(blank=True, null=True)

    cache = cache.ActionCache()
    # previous_version = 'sa_api_v1.models.Activity'

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_activity'
        ordering = ['-created_datetime']

    @property
    def submitter(self):
        return self.thing.submitter


def timestamp_filename(attachment, filename):
    # NOTE: It would be nice if this were a staticmethod in Attachment, but
    # Django 1.4 tries to convert the function to a string when we do that.
    return ''.join(['attachments/', utils.base62_time(), '-', filename])

AttachmentStorage = get_storage_class(settings.ATTACHMENT_STORAGE)


class Attachment (CacheClearingModel, TimeStampedModel):
    """
    A file attached to a submitted thing.
    """
    file = models.FileField(upload_to=timestamp_filename, storage=AttachmentStorage())
    name = models.CharField(max_length=128, null=True, blank=True)
    thing = models.ForeignKey('SubmittedThing', related_name='attachments')

    cache = cache.AttachmentCache()
    # previous_version = 'sa_api_v1.models.Attachment'

    class Meta:
        app_label = 'sa_api_v2'
        db_table = 'sa_api_attachment'


#
