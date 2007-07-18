from datetime import datetime, timedelta

from django.db import models, transaction
from django.contrib import admin
from django.utils.timesince import timesince
from django import newforms as forms
from django.core.urlresolvers import reverse
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.contrib.sites.managers import CurrentSiteManager
from django.contrib.sites.models import Site
from django.template.defaultfilters import slugify

from ella.core.cache import get_cached_object

DEFAULT_LISTING_PRIORITY = getattr(settings, 'DEFAULT_LISTING_PRIORITY', 0)

class Author(models.Model):
    user = models.ForeignKey(User, blank=True, null=True)
    name = models.CharField(_('Name'), maxlength=200, blank=True)
    slug = models.CharField(_("Slug"), maxlength=200)
    photo = models.ImageField(_('Photo'), upload_to='photos/%Y/%m/%d', blank=True)
    description = models.TextField(_('Description'), blank=True)
    text = models.TextField(_('Text'), blank=True)

    def __unicode__(self):
        return self.name

    class Meta:
        ordering=('name',)
        verbose_name = _('Author')
        verbose_name_plural = _('Authors')

class Source(models.Model):
    name = models.CharField(_('Name'), maxlength=200)
    url = models.URLField(_('URL'), blank=True)
    description = models.TextField(_('Description'), blank=True)

    def __unicode__(self):
        return self.name

    class Meta:
        verbose_name = _('Source')
        verbose_name_plural = _('Sources')
        ordering = ('name',)

CATEGORY_CACHE = {}
class CategoryManager(CurrentSiteManager):

    def clear_cache(self):
        global CATEGORY_CACHE
        CATEGORY_CACHE = {}

    def get_by_slug(self, slug):
        try:
            category = CATEGORY_CACHE[slug]
        except KeyError:
            category = self.get(slug=slug)
            CATEGORY_CACHE[slug] = category
        return category


class Category(models.Model):
    title = models.CharField(_("Category Title"), maxlength=200)
    slug = models.CharField(_("Slug"), maxlength=200)
    tree_parent = models.ForeignKey('self', null=True, blank=True, verbose_name=_("Parent Category"))
    tree_path = models.CharField(maxlength=255, editable=False, unique=True)
    description = models.TextField(_("Category Description"), blank=True)
    site = models.ForeignKey(Site)

    objects = CategoryManager(field_name='site')
    all_objects = models.Manager()

    @transaction.commit_on_success
    def save(self):
        old_tree_path = self.tree_path
        if self.tree_parent:
            self.tree_path = '%s/%s' % (self.tree_parent.tree_path, self.slug)
        else:
            self.tree_path = '%s' % self.slug
        super(Category, self).save()
        Category.objects.clear_cache()
        if old_tree_path != self.tree_path:
            children = Category.objects.filter(tree_path__startswith=old_tree_path)
            for child in children:
                child.tree_path = child.tree_path.replace(old_tree_path, self.tree_path)
                child.save()

    def draw_title(self):
        return ('&nbsp;&nbsp;' * self.tree_path.count('/')) + self.title
    draw_title.allow_tags = True

    class Meta:
        ordering = ('tree_path', 'title',)
        verbose_name = _('Category')
        verbose_name_plural = _('Categories')

    def __unicode__(self):
        return self.title

class ListingManager(models.Manager):
    NONE = 0
    IMMEDIATE = 1
    ALL = 2

    def clean_listings(self):
        """
        Method that cleans the Listing model by deleting all listings that are no longer valid.
        Should be run periodicaly to purge the DB from unneeded data.
        """
        self.filter(remove=True, priority_to__lte=datetime.now()).delete()

    def get_queryset(self, category=None, children=NONE, mods=[], content_types=[], **kwargs):
        now = datetime.now()
        qset = self.exclude(remove=True, priority_to__lte=datetime.now()).filter(publish_from__lte=now, **kwargs)

        if category:
            if children == self.NONE:
                # only this one category
                qset = qset.filter(category=category)
            elif children == self.IMMEDIATE:
                # this category and its children
                qset = qset.filter(models.Q(category__tree_parent=category) | models.Q(category=category))
            else:
                # this category and all its descendants
                qset = qset.filter(category__tree_path__startswith=category.tree_path)

        # filtering based on Model classes
        if mods:
            qset = qset.filter(target_ct__in=([ ContentType.objects.get_for_model(m) for m in mods ] + content_types))

        return qset

    def get_count(self, category=None, children=NONE, mods=[], **kwargs):
        return self.get_queryset(category, children, mods, **kwargs).count()

    # FIXME add caching
    def get_listing(self, category=None, count=10, offset=1, children=NONE, mods=[], content_types=[], **kwargs):
        """
        Get top objects for given category and potentionally also its child categories.

        Params:
            category - Category object to list objects for. None if any category will do
            count - number of objects to output, defaults to 10
            offset - starting with object number... 1-based
            children - one of
                            NONE: only this category
                            IMMEDIATE: this category and its immediate children
                            ALL: all descendants from the given category
            mods - list of Models, if empty, object from all models are included
            **kwargs - rest of the parameter are passed to the queryset unchanged
        """
        assert offset > 0, "Offset must be a positive integer"
        assert count > 0, "Count must be a positive integer"

        now = datetime.now()
        qset = self.get_queryset(category, children, mods, content_types, **kwargs).select_related()

        # listings with active priority override
        active = models.Q(priority_from__isnull=False, priority_from__lte=now, priority_to__gte=now)

        qsets = (
            # modded-up objects
            qset.filter(active, priority_value__gt=DEFAULT_LISTING_PRIORITY).order_by('-priority_value', '-publish_from'),
            # default priority
            qset.exclude(active).order_by('-publish_from'),
            # modded-down priority
            qset.filter(active, priority_value__lt=DEFAULT_LISTING_PRIORITY).order_by('-priority_value', '-publish_from'),
)

        out = []

        # templates are 1-based, compensate
        offset -= 1

        # iterate through qsets until we have enough objects
        for q in qsets:
            data = q[offset:offset+count]
            if data:
                offset = 0
                out.extend(data)
                count -= len(data)
                if count <= 0:
                    break
            elif offset != 0:
                offset -= q.count()
        return out

class Listing(models.Model):
    target_ct = models.ForeignKey(ContentType)
    target_id = models.IntegerField()

    @property
    def target(self):
        if not hasattr(self, '_target'):
            self._target = get_cached_object(self.target_ct, pk=self.target_id)
        return self._target

    category = models.ForeignKey(Category, db_index=True)

    publish_from = models.DateTimeField(_("Start of listing"), default=datetime.now)
    priority_from = models.DateTimeField(_("Start of prioritized listing"), default=datetime.now, null=True, blank=True)
    priority_to = models.DateTimeField(_("End of prioritized listing"), default=lambda: datetime.now() + timedelta(days=7), null=True, blank=True)
    priority_value = models.IntegerField(_("Priority"), default=DEFAULT_LISTING_PRIORITY, blank=True)
    remove = models.BooleanField(_("Remove"), help_text=_("Remove object from listing after the priority wears off?"), default=False)

    objects = ListingManager()

    def get_absolute_url(self):
        obj = self.target
        return reverse(
                'object_detail',
                kwargs={
                    'category' : get_cached_object(
                                ContentType.objects.get_for_model(Category),
                                pk=getattr(obj, 'category_id', self.category_id)
).tree_path,
                    'year' : self.publish_from.year,
                    'month' : self.publish_from.month,
                    'day' : self.publish_from.day,
                    'content_type' : slugify(obj._meta.verbose_name_plural),
                    'slug' : obj.slug,
}
)

    def save(self):
        # do not allow prioritizations without a priority_value
        if self.priority_value == DEFAULT_LISTING_PRIORITY:
            self.priority_from = None
        super(Listing, self).save()

    def __unicode__(self):
        return u'%s listed in %s' % (self.target, self.category)

    @property
    def priority(self):
        """
        Get the actual priority according to settings.
        """
        now = datetime.now()
        if now > self.priority_to and self.remove:
            return None
        elif self.priority_from <= now <= self.priority_to:
            return self.priority_value
        else:
            return DEFAULT_LISTING_PRIORITY

    class Meta:
        verbose_name = _('Listing')
        verbose_name_plural = _('Listings')
        ordering = ('-publish_from',)
        unique_together = (('category', 'target_id', 'target_ct'),)

class Dependency(models.Model):
    target_ct = models.ForeignKey(ContentType, related_name='dependency_for_set')
    target_id = models.IntegerField()

    source_ct = models.ForeignKey(ContentType, related_name='dependent_on_set')
    source_id = models.IntegerField()

    def __unicode__(self):
        return u'%s depends on %s' % (self.source, self.target)

    @property
    def source(self):
        if not hasattr(self, '_source'):
            self._source = get_cached_object(self.source_ct, pk=self.source_id)
        return self._source

    @property
    def target(self):
        if not hasattr(self, '_target'):
            self._target = get_cached_object(self.target_ct, pk=self.target_id)
        return self._target

    class Meta:
        verbose_name = _('Dependency')
        verbose_name_plural = _('Dependencies')
        ordering = ('source_ct', 'source_id',)

import widgets
class ListingOptions(admin.ModelAdmin):
    raw_id_fields = ('target_id',)
    list_display = ('target', 'category', 'publish_from',)
    list_filter = ('publish_from',)

    def formfield_for_dbfield(self, db_field, **kwargs):
        if db_field.name in ('target_ct',):
            return forms.ModelChoiceField(
                    queryset=ContentType.objects.all(),
                    widget=widgets.ContentTypeWidget(db_field.name.replace('_ct', '_id')),
)
        elif db_field.name == 'target_id':
            return super(ListingOptions, self).formfield_for_dbfield(db_field, widget=widgets.ForeignKeyRawIdWidget , **kwargs)

        return super(ListingOptions, self).formfield_for_dbfield(db_field, **kwargs)

class DependencyOptions(admin.ModelAdmin):
    raw_id_fields = ('target_id', 'source_id',)

    def formfield_for_dbfield(self, db_field, **kwargs):
        if db_field.name in ('target_ct', 'source_ct'):
            return forms.ModelChoiceField(
                    queryset=ContentType.objects.all(),
                    widget=widgets.ContentTypeWidget(db_field.name.replace('_ct', '_id')),
)
        elif db_field.name in ('target_id', 'source_id'):
            return super(DependencyOptions, self).formfield_for_dbfield(db_field, widget=widgets.ForeignKeyRawIdWidget , **kwargs)

        return super(DependencyOptions, self).formfield_for_dbfield(db_field, **kwargs)

class CategoryOption(admin.ModelAdmin):
    list_display = ('draw_title', 'tree_path')
    ordering = ('tree_path',)
    prepopulated_fields = {'slug': ('title',)}

class AuthorOptions(admin.ModelAdmin):
    prepopulated_fields = {'slug': ('name',)}

admin.site.register(Category, CategoryOption)
admin.site.register(Source)
admin.site.register(Author, AuthorOptions)
admin.site.register(Listing, ListingOptions)
admin.site.register(Dependency , DependencyOptions)
