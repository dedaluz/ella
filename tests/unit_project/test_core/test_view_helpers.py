# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from djangosanetesting import DatabaseTestCase, UnitTestCase

from django.http import Http404
from django.contrib.contenttypes.models import ContentType
from django.template.defaultfilters import slugify

from ella.core.views import _category_detail, _object_detail, get_content_type, _list_content_type
from ella.core.models import Listing

from unit_project.test_core import create_basic_categories, create_and_place_a_publishable, \
        create_and_place_more_publishables, list_all_placements_in_category_by_hour

class ViewHelpersTestCase(DatabaseTestCase):
    def setUp(self):
        super(ViewHelpersTestCase, self).setUp()
        create_basic_categories(self)
        create_and_place_a_publishable(self)
        self.user = self
        setattr(self.user, 'is_staff', False)

class TestGetContentType(UnitTestCase):
    def test_by_brute_force(self):
        for ct in ContentType.objects.all():
            self.assert_equals(ct, get_content_type(slugify(ct.model_class()._meta.verbose_name_plural)))

    def test_raises_404_on_non_existing_model(self):
        self.assert_raises(Http404, get_content_type, '')

class TestCategoryDetail(ViewHelpersTestCase):
    def test_returns_category_by_tree_path(self):
        c = _category_detail('nested-category')
        self.assert_equals(self.category_nested, c['category'])
        self.assert_false(c['is_homepage'])
        
    def test_returns_home_page_with_no_args(self):
        c = _category_detail()
        self.assert_equals(self.category, c['category'])
        self.assert_true(c['is_homepage'])
        
    def test_returns_nested_category_by_tree_path(self):
        c = _category_detail('nested-category/second-nested-category')
        self.assert_equals(self.category_nested_second, c['category'])
        self.assert_false(c['is_homepage'])

class TestObjectDetail(ViewHelpersTestCase):
    def setUp(self):
        super(TestObjectDetail, self).setUp()
        self.correct_args = [self.user, 'nested-category', 'articles', 'first-article', '2008', '1', '10']

    def test_raises_404_on_incorrect_category(self):
        self.correct_args[1] = 'not-an-existing-category'
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_raises_404_on_wrong_category(self):
        self.correct_args[1] = ''
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_raises_404_on_incorrect_content_type(self):
        self.correct_args[2] = 'not-a-content-type'
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_raises_404_on_incorrect_slug(self):
        self.correct_args[3] = 'not-an-existing-slug'
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_raises_404_on_incorrect_date(self):
        self.correct_args[4] = '2000'
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_returns_correct_context(self):
        c = _object_detail(*self.correct_args)

        self.assert_equals(5, len(c.keys()))
        self.assert_equals(self.publishable, c['object'])
        self.assert_equals(self.placement, c['placement'])
        self.assert_equals(self.category_nested, c['category'])
        self.assert_equals('articles', c['content_type_name'])
        self.assert_equals(self.publishable.content_type, c['content_type'])

    def test_doesnt_match_static_placement_if_date_is_supplied(self):
        self.placement.static = True
        self.placement.save()
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_doesnt_match_placement_if_date_is_not_supplied(self):
        self.correct_args = self.correct_args[:4]
        self.assert_raises(Http404, _object_detail, *self.correct_args)

    def test_matches_static_placement_if_date_is_not_supplied(self):
        self.placement.static = True
        self.placement.save()
        self.correct_args = self.correct_args[:4]

        c = _object_detail(*self.correct_args)

        self.assert_equals(5, len(c.keys()))
        self.assert_equals(self.publishable, c['object'])
        self.assert_equals(self.placement, c['placement'])
        self.assert_equals(self.category_nested, c['category'])
        self.assert_equals('articles', c['content_type_name'])
        self.assert_equals(self.publishable.content_type, c['content_type'])

class TestListContentType(ViewHelpersTestCase):
    def setUp(self):
        super(TestListContentType, self).setUp()
        create_and_place_more_publishables(self)
        list_all_placements_in_category_by_hour(self, category=self.category)

    def test_only_category_and_year_returns_all_listings(self):
        c = _list_content_type('', '2008')
        self.assert_equals(self.listings, c['listings'])
        
    def test_only_nested_category_and_year_returns_all_listings(self):
        Listing.objects.all().update(category=self.category_nested_second)
        c = _list_content_type('nested-category/second-nested-category', '2008')
        self.assert_equals(self.listings, c['listings'])

    def test_return_first_2_listings_if_paginate_by_2(self):
        c = _list_content_type('', '2008', paginate_by=2)
        self.assert_equals(self.listings[:2], c['listings'])
        self.assert_true(c['is_paginated'])
        
    def test_return_second_2_listings_if_paginate_by_2_and_page_2(self):
        c = _list_content_type('', '2008', page_no=2, paginate_by=2)
        self.assert_equals(self.listings[2:4], c['listings'])
        self.assert_true(c['is_paginated'])

    def test_reflect_priorities(self):
        l = self.listings[-1]
        l.priority_value = 10
        l.priority_from = datetime.now() - timedelta(days=1)
        l.priority_to = datetime.now() + timedelta(days=1)
        l.save()

        c = _list_content_type('', '2008')
        expected = [l] + self.listings[:-1]
        self.assert_equals(expected, c['listings'])

    def test_returns_empty_list_if_no_listing_found(self):
        c = _list_content_type('', '2007')
        self.assert_equals([], c['listings'])
        
    def test_raises404_for_incorrect_category(self):
        self.assert_raises(Http404, _list_content_type, 'XXX', '2008')

    def test_raises404_for_incorrect_month(self):
        self.assert_raises(Http404, _list_content_type, '', '2008', '13')

    def test_raises404_for_incorrect_day(self):
        self.assert_raises(Http404, _list_content_type, '', '2008', '1', '42')

    def test_raises404_for_incorrect_date(self):
        self.assert_raises(Http404, _list_content_type, '', '2008', '2', '30')

    def test_raises404_for_incorrect_content_type(self):
        self.assert_raises(Http404, _list_content_type, '', '2008', '2', '3', 'not-a-content-type')


