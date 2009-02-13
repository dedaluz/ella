from django.db import models
from django.utils.translation import ugettext_lazy as _, ugettext
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import User, Group, Permission
from django.conf import settings

from ella.core.cache.utils import CachedForeignKey
from ella.core.models import Category

class DevMessage(models.Model):
    """Development news for ella administrators."""

    title = models.CharField(_('Title'), max_length=255)
    slug = models.SlugField(_('Slug'), max_length=32)
    summary = models.TextField(_('Summary'))
    details = models.TextField(_('Detail'), blank=True)
    version = models.CharField(_('Version'), max_length=32)

    author = models.ForeignKey(User, editable=False)
    ts = models.DateTimeField(editable=False, auto_now_add=True)

    def __unicode__(self):
        return self.title

    class Meta:
        verbose_name = _('Development message')
        verbose_name_plural = _('Development messages')
        ordering = ('-ts',)
        unique_together = (('slug', 'ts',),)


class AdminHelpItem(models.Model):
    """Help for ella administrators, that extends django help_text functionality."""

    ct = CachedForeignKey(ContentType, verbose_name=_('Model'))
    field = models.CharField(_('Field'), max_length=64, blank=True)
    lang = models.CharField(_('Language'), max_length=5, choices=settings.LANGUAGES)
    short = models.CharField(_('Short help'), max_length=255)
    long = models.TextField(_('Full message'), blank=True)

    def __unicode__(self):
        if self.field:
            return u"%s: %s" % (self.ct, self.field)
        return u"%s: %s" % (self.ct, _('General'))

    class Meta:
        verbose_name = _('Help item')
        verbose_name_plural = _('Help items')
        ordering = ('ct', 'field',)
        unique_together = (('ct', 'field',),)


class AdminUserDraft(models.Model):
    """Here is auto-saved objects and user templates."""

    ct = CachedForeignKey(ContentType, verbose_name=_('Model'))
    user = CachedForeignKey(User, verbose_name=_('User'))
    data = models.TextField(_('Data')) # TODO: JSONField

    # If it's template, some info about it
    title = models.CharField(_('Title'), max_length=64, blank=True)
    slug = models.SlugField(_('Slug'), max_length=64, blank=True)

    is_template = models.BooleanField(_('Is template'), default=False)
    ts = models.DateTimeField(editable=False, auto_now_add=True)

    def __unicode__(self):
        if self.is_template:
            return self.title
        return "Autosaved %s (%s)" % (self.ct, self.ts)

    class Meta:
        verbose_name = _('Draft item')
        verbose_name_plural = _('Draft items')


class AdminSetting(models.Model):
    """Custom settings for newman users/groups"""

    user = CachedForeignKey(User, verbose_name=_('User'), null=True, blank=True)
    group = CachedForeignKey(Group, verbose_name=_('Group'), null=True, blank=True)
    var = models.SlugField(_('Variable'), max_length=64)
    val = models.TextField(_('Value')) # TODO: JSONField with validation...

    def __unicode__(self):
        return u"%s: %s for %s" % (self.var, self.val, self.user)

    class Meta:
        unique_together = (('user','var',),('group', 'var',),)
        verbose_name = _('Admin user setting')
        verbose_name_plural = _('Admin user settings')


class AdminGroupFav(models.Model):
    """Admin favorite items per group (presets)."""

    ct = CachedForeignKey(ContentType, verbose_name=_('Model'))
    group = CachedForeignKey(Group, verbose_name=_('Group'))
    ordering = models.PositiveSmallIntegerField(_('Ordering'))

    def __unicode__(self):
        return "%s - %s" % (self.group, self.ct)

    class Meta:
        unique_together = (('ct', 'group',),)
        ordering = ('ordering',)
        verbose_name = _('Group fav item')
        verbose_name_plural = _('Group fav items')


class AdminUserFav(models.Model):
    """Admin favorite items per user."""

    ct = CachedForeignKey(ContentType, verbose_name=_('Model'))
    user = CachedForeignKey(User, verbose_name=_('User'))
    ordering = models.PositiveSmallIntegerField(_('Ordering'))

    def __unicode__(self):
        return "%s - %s" % (self.user, self.ct)

    class Meta:
        unique_together = (('ct', 'user',),)
        ordering = ('ordering',)
        verbose_name = _('User fav item')
        verbose_name_plural = _('User fav items')


# ------------------------------------
# Permissions per Category
# ------------------------------------

class CategoryUserRole(models.Model):
    """
    Apply all group's permission for the given user to this category.
    """
    user = models.ForeignKey(User, limit_choices_to={'is_superuser': False})
    group = models.ForeignKey(Group)
    category = models.ForeignKey(Category)

    def __unicode__(self):
        return ugettext(u'User %(user)s is a %(group)s for %(category)s.') % {
                'user' : self.user,
                'group' : self.group,
                'category' : self.category,
}

    class Meta:
        verbose_name = _("User role in category")
        verbose_name_plural = _("User roles in categories")

def has_category_permission(user, model, category, permission):
    if user.has_perm(permission):
        return True

    app_label, code = permission.split('.', 1)
    perm = Permission.objects.filter(content_type__app_label=app_label, codename=code)

    if CategoryUserRole.objects.filter(
            category=category,
            user=user,
            group__permissions=perm
).count():
        return True

    # fallback to site permissions
    return has_site_permission(user, model, category.site_id, permission)

def cat_children(cats):
    """ Returns all nested categories as list. cats parameter is list or tuple. """
    # TODO cache result of this function or rewrite this function smarter or maybe query all nested categories via SQL(?).
    sub_cats = []
    for c in cats:
        out = Category.objects.filter(tree_parent=c)
        if out:
            map(lambda o: sub_cats.append(o), out)
            nested = cat_children(out)
            if nested:
                map(lambda n: sub_cats.append(n), nested)
    return sub_cats

def applicable_categories(user, permission=None):
    from time import time
    begin = time()
    q = CategoryUserRole.objects.filter(user=user).distinct()
    cats = [ a.category for a in q ]
    app_cats = cat_children(cats)

    if permission:
        app_label, code = permission.split('.', 1)
        perm = Permission.objects.get(content_type__app_label=app_label, codename=code)
        q = q.filter(group__permissions=perm)
    else:
        # take any permission
        q = q.filter(group__permissions__id__isnull=False)

    print 'Took %f' % (time() - begin)
    return [ d.pk for d in app_cats ]

