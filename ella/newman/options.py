from django.core.exceptions import PermissionDenied
from django.conf import settings
from ella.ellaadmin.options import SUGGEST_VIEW_MIN_LENGTH, SUGGEST_VIEW_LIMIT,\
    SUGGEST_RETURN_ALL_FIELD

from django.contrib import admin
from django.contrib.admin.options import ModelAdmin
from django.contrib.admin.options import IncorrectLookupParameters
from django import template
from django.http import HttpResponse, Http404, HttpResponseRedirect
from django.contrib.admin.views.main import ERROR_FLAG
from django.shortcuts import render_to_response
from django.db.models import Q, query
from django.db.models.fields import FieldDoesNotExist

from ella.newman.changelist import NewmanChangeList, FilterChangeList
from ella.newman import models


class NewmanModelAdmin(ModelAdmin):
    registered_views = []

    def __init__(self, *args, **kwargs):
        super(NewmanModelAdmin, self).__init__(*args, **kwargs)
        if hasattr(settings, 'NEWMAN_LIST_PER_PAGE'):
            self.list_per_page = settings.NEWMAN_LIST_PER_PAGE
        #NewmanModelAdmin.register(lambda x: x is None, self.changelist_view)
        #NewmanModelAdmin.register(lambda x: x.endswith('suggest'), self.suggest_view)
        #NewmanModelAdmin.register(lambda x: x.endswith('filters'), self.filters_view)

    def register(cls, test_callback, view_callback):
        cls.registered_views.append({'test': test_callback, 'view': view_callback})
    register = classmethod(register)

    def __call__(self, request, url):
        for reg in self.registered_views:
            if reg['test'](url):
                return reg['view'](self, request)

        if not url:
            return self.changelist_view(request)
        elif url.endswith('suggest'):
            return self.suggest_view(request)
        elif url.endswith('filters'):
            return self.filters_view(request)
        return super(NewmanModelAdmin, self).__call__(request, url)


    def filters_view(self, request, extra_context=None):
        "stolen from: The 'change list' admin view for this model."
        opts = self.model._meta
        app_label = opts.app_label
        try:
            cl = FilterChangeList(request, self.model, self.list_display, self.list_display_links, self.list_filter,
                self.date_hierarchy, self.search_fields, self.list_select_related, self.list_per_page, self)
        except IncorrectLookupParameters:
            # Wacky lookup parameters were given, so redirect to the main
            # changelist page, without parameters, and pass an 'invalid=1'
            # parameter via the query string. If wacky parameters were given and
            # the 'invalid=1' parameter was already in the query string, something
            # is screwed up with the database, so display an error page.
            if ERROR_FLAG in request.GET.keys():
                return render_to_response('admin/invalid_setup.html', {'title': _('Database error')})
            return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')

        context = {
            'title': cl.title,
            'is_popup': cl.is_popup,
            'cl': cl,
            'has_add_permission': self.has_add_permission(request),
            'root_path': self.admin_site.root_path,
            'app_label': app_label,
}
        context.update(extra_context or {})
        out= render_to_response(
            'admin/filters.html',
            context,
            context_instance=template.RequestContext(request)
)
        return HttpResponse(out, mimetype='text/plain;charset=utf-8')


    def changelist_view(self, request, extra_context=None):
        # accepts only ajax calls if not DEBUG
        if not request.is_ajax() and not settings.DEBUG:
            raise Http404

        opts = self.model._meta
        app_label = opts.app_label
        if not self.has_change_permission(request, None):
            raise PermissionDenied
        try:
            cl = NewmanChangeList(request, self.model, self.list_display, self.list_display_links, self.list_filter,
                self.date_hierarchy, self.search_fields, self.list_select_related, self.list_per_page, self)
        except IncorrectLookupParameters:
            # Wacky lookup parameters were given, so redirect to the main
            # changelist page, without parameters, and pass an 'invalid=1'
            # parameter via the query string. If wacky parameters were given and
            # the 'invalid=1' parameter was already in the query string, something
            # is screwed up with the database, so display an error page.
            if ERROR_FLAG in request.GET.keys():
                return render_to_response('admin/invalid_setup.html', {'title': _('Database error')})
            return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')

        context = {
            'title': cl.title,
            'is_popup': cl.is_popup,
            'cl': cl,
            'has_add_permission': self.has_add_permission(request),
            'root_path': self.admin_site.root_path,
            'app_label': app_label,
}
        context.update(extra_context or {})
        return render_to_response(self.change_list_template or [
            'admin/%s/%s/change_list.html' % (app_label, opts.object_name.lower()),
            'admin/%s/change_list.html' % app_label,
            'admin/change_list.html'
        ], context, context_instance=template.RequestContext(request))


    def suggest_view(self, request, extra_context=None):
        # accepts only ajax calls if not DEBUG
        if not request.is_ajax() and not settings.DEBUG:
            raise Http404

        if not ('f' in request.GET.keys() and 'q' in request.GET.keys()):
            raise AttributeError, 'Invalid query attributes. Example: ".../?f=field_a&f=field_b&q=search_term&o=offset"'
        elif len(request.GET.get('q')) < SUGGEST_VIEW_MIN_LENGTH:
            return HttpResponse('', mimetype='text/plain;charset=utf-8')

        offset = 0
        if 'o' in request.GET.keys() and request.GET.get('o'):
            offset = int(request.GET.get('o'))
        limit = offset + SUGGEST_VIEW_LIMIT

        lookup_fields = [u'id'] + request.GET.getlist('f')
        lookup_value = request.GET.get('q')
        lookup = None

        model_fields = [f.name for f in self.model._meta.fields]

        for f in lookup_fields:

            if not (f in model_fields or f.split('__')[0] in model_fields):
                raise AttributeError, 'Model "%s" has not field "%s". Possible fields are "%s".' \
                                    % (self.model._meta.object_name, f, ', '.join(model_fields))
            lookup_key = str('%s__icontains' % f)
            if not lookup:
                lookup = Q(**{lookup_key: lookup_value})
            else:
                lookup = lookup | Q(**{lookup_key: lookup_value})

        if SUGGEST_RETURN_ALL_FIELD:
            data = self.model.objects.filter(lookup).values(*lookup_fields)
        else:
            data = self.model.objects.filter(lookup).values(*lookup_fields[:2])

        cnt = len(data)

        # sort the suggested items so that those starting with the sought term come first
        def compare(a,b):
            def _cmp(a,b,sought):
                a_starts = unicode(a).lower().startswith(sought)
                b_starts = unicode(b).lower().startswith(sought)
                # if exactly one of (a,b) starts with sought, the one starting with it comes first
                if a_starts ^ b_starts:
                    if a_starts: return -1
                    if b_starts: return +1
                # else compare lexicographically
                return cmp(a,b)
            return _cmp(a,b,unicode(lookup_value).lower())
        data = list(data)
        if offset >= len(data): return HttpResponse('SPECIAL: OFFSET OUT OF RANGE', mimetype='text/plain')
        data.sort(cmp=compare, key=lambda x: x[lookup_fields[1]])
        data = data[offset:limit]

        ft = []
        ft.append('{cnt:%d}' % cnt)
        for item in data:
            if SUGGEST_RETURN_ALL_FIELD:
                ft.append("%s".encode('utf-8') % '|'.join("%s" % item[f] for f in lookup_fields))
            else:
                ft.append("%s".encode('utf-8') % '|'.join("%s" % item[f] for f in lookup_fields[:2]))

        return HttpResponse('\n'.join(ft), mimetype='text/plain;charset=utf-8')


    def queryset(self, request):
        """
        First semi-working draft of category-based permissions. It will allow permissions to be set per category
        effectively hiding the content the user has no permission to see/change.
        """
        if request.user.is_superuser:
            return super(NewmanModelAdmin, self).queryset(request)
        q = admin.ModelAdmin.queryset(self, request)

        view_perm = self.opts.app_label + '.' + 'view_' + self.model._meta.module_name.lower()
        change_perm = self.opts.app_label + '.' + 'change_' + self.model._meta.module_name.lower()
        try:
            self.model._meta.get_field('category')
            categories = models.applicable_categories(request.user, view_perm) + models.applicable_categories(request.user, change_perm)

            if categories:
                # TODO: terrible hack for circumventing invalid Q(__in=[]) | Q(__in=[])
                q = q.filter(Q(category__in=categories))
            else:
                q = query.EmptyQuerySet()
        except FieldDoesNotExist:
            pass
        return q

    def has_change_permission(self, request, obj=None):
        """
        Returns True if the given request has permission to change the given
        Django model instance.

        If `obj` is None, this should return True if the given request has
        permission to change *any* object of the given type.
        """
        if obj is None or not hasattr(obj, 'category'):
            return admin.ModelAdmin.has_change_permission(self, request, obj)
        opts = self.opts
        return models.has_category_permission(request.user, obj, obj.category, opts.app_label + '.' + opts.get_change_permission())
