"""
Models and managers for generic tagging.
"""
import re
import logging

from django.contrib.contenttypes import generic
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models
from django.db.models.query import QuerySet
from django.utils.translation import ugettext_lazy as _

from ella.core.models.main import Category

from ella.tagging import settings
from ella.tagging.utils import calculate_cloud, get_tag_list, get_queryset_and_model, parse_tag_input
from ella.tagging.utils import LOGARITHMIC, PRIMARY_TAG, TAG_DELIMITER
from ella.tagging.validators import isTag
from ella.core.cache.utils import get_cached_list, cache_this, delete_cached_object, CachedGenericForeignKey
from ella.core.cache.invalidate import CACHE_DELETER

try:
    import cPickle as pickle
except ImportError:
    import pickle

qn = connection.ops.quote_name
parse_lookup = None
log = logging.getLogger('ella.tagging')
CACHE_TIMEOUT_SHORT = getattr(settings, 'CACHE_TIMEOUT_SHORT', 60)

def get_key_get_for_object(func, self, obj, **kwargs):
    if not isinstance(obj, models.Model):
        raise Exception('cannot calculate key for tag cloud when obj is not django ORM model')
    out = 'ella.tagging.models.TagManager.get_for_object:%d' \
        % (obj.pk)
    return out

def get_key_cloud_for_category(func, self, category, **kwargs):
    key_id = -1
    if category:
        key_id = category.pk
    out = 'ella.tagging.models.TagManager.cloud_for_category:%d:%d' \
        % (key_id, kwargs.get('priority', PRIMARY_TAG))
    return out

def get_key_cloud_for_model(func, self, model, steps=4, distribution=LOGARITHMIC,
                            filters=None, min_count=None):
    ct_model = ContentType.objects.get_for_model(model)
    out = 'ella.tagging.models.TagManager.cloud_for_model:%d:%d:%d:%s:%s' \
        % (ct_model.pk, steps, distribution, str(filters), str(min_count))
    return out

class WrongTagFormat(Exception):
    pass

############
# Managers #
############

class TagManager(models.Manager):
    def update_tags(self, obj, tag_names):
        """
        Update tags associated with an object.
        """
        ctype = ContentType.objects.get_for_model(obj)
        current_tags = list(self.filter(items__content_type__pk=ctype.pk,
                                        items__object_id=obj.pk))
        updated_tag_names = parse_tag_input(tag_names)
        if settings.FORCE_LOWERCASE_TAGS:
            updated_tag_names = [t.lower() for t in updated_tag_names]

        # Remove tags which no longer apply
        tags_for_removal = [tag for tag in current_tags \
                            if tag.name not in updated_tag_names]
        if len(tags_for_removal):
            TaggedItem._default_manager.filter(content_type__pk=ctype.pk,
                                               object_id=obj.pk,
                                               tag__in=tags_for_removal).delete()
        # Add new tags
        current_tag_names = [tag.name for tag in current_tags]
        for tag_name in updated_tag_names:
            if tag_name not in current_tag_names:
                tag, created = self.get_or_create(name=tag_name)
                TaggedItem._default_manager.create(tag=tag, object=obj)

    def add_tag(self, obj, tag_name, tag_priority=PRIMARY_TAG):
        """
        Associates the given object with a tag.
        """
        tag_name = tag_name.strip()
        if not tag_name:
            raise AttributeError(_('No tags were given: "%s".') % tag_name)
        if settings.FORCE_LOWERCASE_TAGS:
            tag_name = tag_name.lower()
        if re.findall(r'[^\w^\s^%s]' % TAG_DELIMITER, tag_name, re.UNICODE):
            raise WrongTagFormat
        if len(tag_name.split(TAG_DELIMITER)) > 1:
            raise AttributeError(_('Multiple tags were given: "%s".') % tag_name)
        tag_name = tag_name.strip()
        tag, created = self.get_or_create(name=tag_name)
        ctype = ContentType.objects.get_for_model(obj)
        cat = None
        if hasattr(obj, 'get_category'):
            cat = obj.get_category()
            #inv_key = get_key_cloud_for_category(None, self, cat, priority=tag_priority) # cache key for category tag cloud.
            #delete_cached_object(inv_key)  # invalidate cached clouds for category ``cat`` TODO .. delete from cache via invalidator
        #inv_key = get_key_get_for_object(None, self, obj) # invalidate tags returned by TagManager.get_for_object
        #delete_cached_object(inv_key) # TODO delete from cache via invalidator
        return TaggedItem._default_manager.get_or_create(
            tag=tag,
            content_type=ctype,
            object_id=obj.pk,
            category=cat,
            priority=tag_priority,
)

    #@cache_this(get_key_get_for_object, None)
    def get_for_object(self, obj, **kwargs):
        """
        Create a queryset matching all tags associated with the given
        object.
        """
        ctype = ContentType.objects.get_for_model(obj)
        prio = kwargs.get('priority', None)
        kw = {
            'items__content_type__pk': ctype.pk,
            'items__object_id': obj.pk
}
        if prio:
            kw['items__priority'] = prio
        # make list form QuerySet object because of problems with saving QS into memcached TODO solve the problem!
        return list(Tag.objects.filter(**kw).all())

    def _get_usage(self, model, counts=False, min_count=None, extra_joins=None, extra_criteria=None, params=None):
        """
        Perform the custom SQL query for ``usage_for_model`` and
        ``usage_for_queryset``.
        """
        if min_count is not None: counts = True

        model_table = qn(model._meta.db_table)
        model_pk = '%s.%s' % (model_table, qn(model._meta.pk.column))
        query = """
        SELECT DISTINCT %(tag)s.id, %(tag)s.name%(count_sql)s
        FROM
            %(tag)s
            INNER JOIN %(tagged_item)s
                ON %(tag)s.id = %(tagged_item)s.tag_id
            INNER JOIN %(model)s
                ON %(tagged_item)s.object_id = %(model_pk)s
            %%s
        WHERE %(tagged_item)s.content_type_id = %(content_type_id)s
            %%s
        GROUP BY %(tag)s.id, %(tag)s.name
        %%s
        ORDER BY %(tag)s.name ASC""" % {
            'tag': qn(self.model._meta.db_table),
            'count_sql': counts and (', COUNT(%s)' % model_pk) or '',
            'tagged_item': qn(TaggedItem._meta.db_table),
            'model': model_table,
            'model_pk': model_pk,
            'content_type_id': ContentType.objects.get_for_model(model).pk,
}

        min_count_sql = ''
        if min_count is not None:
            min_count_sql = 'HAVING COUNT(%s) >= %%s' % model_pk
            params.append(min_count)

        cursor = connection.cursor()
        cursor.execute(query % (extra_joins, extra_criteria, min_count_sql), params)
        tags = []
        for row in cursor.fetchall():
            t = self.model(*row[:2])
            if counts:
                t.count = row[2]
            tags.append(t)
        return tags

    def usage_for_model(self, model, counts=False, min_count=None, filters=None):
        """
        Obtain a list of tags associated with instances of the given
        Model class.

        If ``counts`` is True, a ``count`` attribute will be added to
        each tag, indicating how many times it has been used against
        the Model class in question.

        If ``min_count`` is given, only tags which have a ``count``
        greater than or equal to ``min_count`` will be returned.
        Passing a value for ``min_count`` implies ``counts=True``.

        To limit the tags (and counts, if specified) returned to those
        used by a subset of the Model's instances, pass a dictionary
        of field lookups to be applied to the given Model as the
        ``filters`` argument.
        """
        if filters is None: filters = {}

        # post-queryset-refactor (hand off to usage_for_queryset)
        queryset = model._default_manager.filter()
        for f in filters.items():
            queryset.query.add_filter(f)
        usage = self.usage_for_queryset(queryset, counts, min_count)

        return usage

    def usage_for_queryset(self, queryset, counts=False, min_count=None):
        """
        Obtain a list of tags associated with instances of a model
        contained in the given queryset.

        If ``counts`` is True, a ``count`` attribute will be added to
        each tag, indicating how many times it has been used against
        the Model class in question.

        If ``min_count`` is given, only tags which have a ``count``
        greater than or equal to ``min_count`` will be returned.
        Passing a value for ``min_count`` implies ``counts=True``.
        """
        if parse_lookup:
            raise AttributeError("'TagManager.usage_for_queryset' is not compatible with pre-queryset-refactor versions of Django.")

        extra_joins = ' '.join(queryset.query.get_from_clause()[0][1:])
        where, params = queryset.query.where.as_sql()
        if where:
            extra_criteria = 'AND %s' % where
        else:
            extra_criteria = ''
        return self._get_usage(queryset.model, counts, min_count, extra_joins, extra_criteria, params)

    def related_for_model(self, tags, model, counts=False, min_count=None):
        """
        Obtain a list of tags related to a given list of tags - that
        is, other tags used by items which have all the given tags.

        If ``counts`` is True, a ``count`` attribute will be added to
        each tag, indicating the number of items which have it in
        addition to the given list of tags.

        If ``min_count`` is given, only tags which have a ``count``
        greater than or equal to ``min_count`` will be returned.
        Passing a value for ``min_count`` implies ``counts=True``.
        """
        if min_count is not None: counts = True
        tags = get_tag_list(tags)
        tag_count = len(tags)
        tagged_item_table = qn(TaggedItem._meta.db_table)
        query = """
        SELECT %(tag)s.id, %(tag)s.name%(count_sql)s
        FROM %(tagged_item)s INNER JOIN %(tag)s ON %(tagged_item)s.tag_id = %(tag)s.id
        WHERE %(tagged_item)s.content_type_id = %(content_type_id)s
          AND %(tagged_item)s.object_id IN
          (
              SELECT %(tagged_item)s.object_id
              FROM %(tagged_item)s, %(tag)s
              WHERE %(tagged_item)s.content_type_id = %(content_type_id)s
                AND %(tag)s.id = %(tagged_item)s.tag_id
                AND %(tag)s.id IN (%(tag_id_placeholders)s)
              GROUP BY %(tagged_item)s.object_id
              HAVING COUNT(%(tagged_item)s.object_id) = %(tag_count)s
)
          AND %(tag)s.id NOT IN (%(tag_id_placeholders)s)
        GROUP BY %(tag)s.id, %(tag)s.name
        %(min_count_sql)s
        ORDER BY %(tag)s.name ASC""" % {
            'tag': qn(self.model._meta.db_table),
            'count_sql': counts and ', COUNT(%s.object_id)' % tagged_item_table or '',
            'tagged_item': tagged_item_table,
            'content_type_id': ContentType.objects.get_for_model(model).pk,
            'tag_id_placeholders': ','.join(['%s'] * tag_count),
            'tag_count': tag_count,
            'min_count_sql': min_count is not None and ('HAVING COUNT(%s.object_id) >= %%s' % tagged_item_table) or '',
}

        params = [tag.pk for tag in tags] * 2
        if min_count is not None:
            params.append(min_count)

        cursor = connection.cursor()
        cursor.execute(query, params)
        related = []
        for row in cursor.fetchall():
            tag = self.model(*row[:2])
            if counts is True:
                tag.count = row[2]
            related.append(tag)
        return related

    #@cache_this(get_key_cloud_for_category, None)
    def cloud_for_category(self, category, **kwargs):
        """ returns tag cloud for all objects in certain category. """
        priority, steps = kwargs.get('priority', PRIMARY_TAG), kwargs.get('steps', 4)
        distribution = kwargs.get('distribution', LOGARITHMIC)
        prio_sql = ''
        if type(priority) == int:
            prio_sql = 'tagging_taggeditem.priority = %d AND' % priority
        elif type(priority) == tuple:
            prio_sql = 'tagging_taggeditem.priority IN %s AND' % str(priority)
        else:
            raise TypeError('kwarg priority should be tuple or int type. %s' % str(type(priority)))
        category_sql = ''
        if category:
            category_sql = 'tagging_taggeditem.category_id = %d AND' % category.pk
        sql = """
        SELECT
            tagging_tag.id, COUNT(tagging_tag.name) AS cnt
        FROM
            tagging_taggeditem,
            tagging_tag
        WHERE
            %s
            %s
            tagging_tag.id = tagging_taggeditem.tag_id
        GROUP BY
            tagging_tag.name
        ORDER BY
            cnt DESC
        """ % (category_sql, prio_sql)
        cur = connection.cursor()
        cur.execute(sql)
        data = cur.fetchall()
        tags = []
        for tag_id, cnt in data:
            o = Tag.objects.get(pk=tag_id)
            setattr(o, 'count', cnt)
            tags.append(o)
        return calculate_cloud(tags, steps, distribution)

    def cloud(self):
        return self.cloud_for_category(None)

    #@cache_this(get_key_cloud_for_model, None, CACHE_TIMEOUT_SHORT)
    def cloud_for_model(self, model, steps=4, distribution=LOGARITHMIC,
                        filters=None, min_count=None):
        """
        Obtain a list of tags associated with instances of the given
        Model, giving each tag a ``count`` attribute indicating how
        many times it has been used and a ``font_size`` attribute for
        use in displaying a tag cloud.

        ``steps`` defines the range of font sizes - ``font_size`` will
        be an integer between 1 and ``steps`` (inclusive).

        ``distribution`` defines the type of font size distribution
        algorithm which will be used - logarithmic or linear. It must
        be either ``tagging.utils.LOGARITHMIC`` or
        ``tagging.utils.LINEAR``.

        To limit the tags displayed in the cloud to those associated
        with a subset of the Model's instances, pass a dictionary of
        field lookups to be applied to the given Model as the
        ``filters`` argument.

        To limit the tags displayed in the cloud to those with a
        ``count`` greater than or equal to ``min_count``, pass a value
        for the ``min_count`` argument.
        """
        tags = list(self.usage_for_model(model, counts=True, filters=filters,
                                         min_count=min_count))
        return calculate_cloud(tags, steps, distribution)

class TaggedItemManager(models.Manager):
    """
    FIXME There's currently no way to get the ``GROUP BY`` and ``HAVING``
          SQL clauses required by many of this manager's methods into
          Django's ORM.

          For now, we manually execute a query to retrieve the PKs of
          objects we're interested in, then use the ORM's ``__in``
          lookup to return a ``QuerySet``.

          Once the queryset-refactor branch lands in trunk, this can be
          tidied up significantly.
    """
    def get_for_object(self, obj, **kwargs):
        """ returns tagged items for object """
        ct = ContentType.objects.get_for_model(obj)
        prio = kwargs.get('priority', None)
        kw = {
            'content_type': ct,
            'object_id': obj._get_pk_val(),
}
        if prio:
            kw['priority'] = prio
        return TaggedItem.objects.filter(**kw)
        #return get_cached_list(TaggedItem, **kw)

    def get_by_model(self, queryset_or_model, tags):
        """
        Create a ``QuerySet`` containing instances of the specified
        model associated with a given tag or list of tags.
        """
        tags = get_tag_list(tags)
        tag_count = len(tags)
        if tag_count == 0:
            # No existing tags were given
            queryset, model = get_queryset_and_model(queryset_or_model)
            return model._default_manager.none()
        elif tag_count == 1:
            # Optimisation for single tag - fall through to the simpler
            # query below.
            tag = tags[0]
        else:
            return self.get_intersection_by_model(queryset_or_model, tags)

        queryset, model = get_queryset_and_model(queryset_or_model)
        content_type = ContentType.objects.get_for_model(model)
        opts = self.model._meta
        tagged_item_table = qn(opts.db_table)
        return queryset.extra(
            tables=[opts.db_table],
            where=[
                '%s.content_type_id = %%s' % tagged_item_table,
                '%s.tag_id = %%s' % tagged_item_table,
                '%s.%s = %s.object_id' % (qn(model._meta.db_table),
                                          qn(model._meta.pk.column),
                                          tagged_item_table)
            ],
            params=[content_type.pk, tag.pk],
)

    def get_intersection_by_model(self, queryset_or_model, tags):
        """
        Create a ``QuerySet`` containing instances of the specified
        model associated with *all* of the given list of tags.
        """
        tags = get_tag_list(tags)
        tag_count = len(tags)
        queryset, model = get_queryset_and_model(queryset_or_model)

        if not tag_count:
            return model._default_manager.none()

        model_table = qn(model._meta.db_table)
        # This query selects the ids of all objects which have all the
        # given tags.
        query = """
        SELECT %(model_pk)s
        FROM %(model)s, %(tagged_item)s
        WHERE %(tagged_item)s.content_type_id = %(content_type_id)s
          AND %(tagged_item)s.tag_id IN (%(tag_id_placeholders)s)
          AND %(model_pk)s = %(tagged_item)s.object_id
        GROUP BY %(model_pk)s
        HAVING COUNT(%(model_pk)s) = %(tag_count)s""" % {
            'model_pk': '%s.%s' % (model_table, qn(model._meta.pk.column)),
            'model': model_table,
            'tagged_item': qn(self.model._meta.db_table),
            'content_type_id': ContentType.objects.get_for_model(model).pk,
            'tag_id_placeholders': ','.join(['%s'] * tag_count),
            'tag_count': tag_count,
}

        cursor = connection.cursor()
        cursor.execute(query, [tag.pk for tag in tags])
        object_ids = [row[0] for row in cursor.fetchall()]
        if len(object_ids) > 0:
            return queryset.filter(pk__in=object_ids)
        else:
            return model._default_manager.none()

    def get_union_by_model(self, queryset_or_model, tags):
        """
        Create a ``QuerySet`` containing instances of the specified
        model associated with *any* of the given list of tags.
        """
        tags = get_tag_list(tags)
        tag_count = len(tags)
        queryset, model = get_queryset_and_model(queryset_or_model)

        if not tag_count:
            return model._default_manager.none()

        model_table = qn(model._meta.db_table)
        # This query selects the ids of all objects which have any of
        # the given tags.
        query = """
        SELECT %(model_pk)s
        FROM %(model)s, %(tagged_item)s
        WHERE %(tagged_item)s.content_type_id = %(content_type_id)s
          AND %(tagged_item)s.tag_id IN (%(tag_id_placeholders)s)
          AND %(model_pk)s = %(tagged_item)s.object_id
        GROUP BY %(model_pk)s""" % {
            'model_pk': '%s.%s' % (model_table, qn(model._meta.pk.column)),
            'model': model_table,
            'tagged_item': qn(self.model._meta.db_table),
            'content_type_id': ContentType.objects.get_for_model(model).pk,
            'tag_id_placeholders': ','.join(['%s'] * tag_count),
}

        cursor = connection.cursor()
        cursor.execute(query, [tag.pk for tag in tags])
        object_ids = [row[0] for row in cursor.fetchall()]
        if len(object_ids) > 0:
            return queryset.filter(pk__in=object_ids)
        else:
            return model._default_manager.none()

    def get_related(self, obj, queryset_or_model, num=None):
        """
        Retrieve a list of instances of the specified model which share
        tags with the model instance ``obj``, ordered by the number of
        shared tags in descending order.

        If ``num`` is given, a maximum of ``num`` instances will be
        returned.
        """
        queryset, model = get_queryset_and_model(queryset_or_model)
        model_table = qn(model._meta.db_table)
        content_type = ContentType.objects.get_for_model(obj)
        related_content_type = ContentType.objects.get_for_model(model)
        query = """
        SELECT %(model_pk)s, COUNT(related_tagged_item.object_id) AS %(count)s
        FROM %(model)s, %(tagged_item)s, %(tag)s, %(tagged_item)s related_tagged_item
        WHERE %(tagged_item)s.object_id = %%s
          AND %(tagged_item)s.content_type_id = %(content_type_id)s
          AND %(tag)s.id = %(tagged_item)s.tag_id
          AND related_tagged_item.content_type_id = %(related_content_type_id)s
          AND related_tagged_item.tag_id = %(tagged_item)s.tag_id
          AND %(model_pk)s = related_tagged_item.object_id"""
        if content_type.pk == related_content_type.pk:
            # Exclude the given instance itself if determining related
            # instances for the same model.
            query += """
          AND related_tagged_item.object_id != %(tagged_item)s.object_id"""
        query += """
        GROUP BY %(model_pk)s
        ORDER BY %(count)s DESC
        %(limit_offset)s"""
        query = query % {
            'model_pk': '%s.%s' % (model_table, qn(model._meta.pk.column)),
            'count': qn('count'),
            'model': model_table,
            'tagged_item': qn(self.model._meta.db_table),
            'tag': qn(self.model._meta.get_field('tag').rel.to._meta.db_table),
            'content_type_id': content_type.pk,
            'related_content_type_id': related_content_type.pk,
            'limit_offset': num is not None and 'LIMIT %d' % num or '',
}

        cursor = connection.cursor()
        cursor.execute(query, [obj.pk])
        object_ids = [row[0] for row in cursor.fetchall()]
        if len(object_ids) > 0:
            # Use in_bulk here instead of an id__in lookup, because id__in would
            # clobber the ordering.
            object_dict = queryset.in_bulk(object_ids)
            return [object_dict[object_id] for object_id in object_ids \
                    if object_id in object_dict]
        else:
            return []

##########
# Models #
##########


class Tag(models.Model):
    """
    A tag.
    """
    name = models.CharField(_('name'), max_length=50, unique=True, db_index=True)

    objects = TagManager()

    class Meta:
        ordering = ('name',)
        verbose_name = _('tag')
        verbose_name_plural = _('tags')

    def __unicode__(self):
        return self.name

    def __cmp__(self, other):
        return cmp(self.name, other.name)

class TaggedItem(models.Model):
    """
    Holds the relationship between a tag and the item being tagged.
    """
    tag          = models.ForeignKey(Tag, verbose_name=_('tag'), related_name='items')
    content_type = models.ForeignKey(ContentType, verbose_name=_('content type'))
    object_id    = models.PositiveIntegerField(_('object id'), db_index=True)
    object       = CachedGenericForeignKey('content_type', 'object_id')
    category     = models.ForeignKey(Category, editable=False, null=True)
    priority     = models.IntegerField(db_index=True)

    objects = TaggedItemManager()

    class Meta:
        # Enforce unique tag association per object
        unique_together = (('tag', 'content_type', 'object_id', 'priority'),)
        verbose_name = _('tagged item')
        verbose_name_plural = _('tagged items')

    def __unicode__(self):
        return u'%s [%s]' % (self.object, self.tag)

