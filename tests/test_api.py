"""
Tests for edx_when.api
"""
from __future__ import absolute_import, unicode_literals

import sys
from datetime import datetime, timedelta

import ddt
import six
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from edx_django_utils.cache.utils import DEFAULT_REQUEST_CACHE
from mock import Mock, call, patch
from opaque_keys.edx.locator import CourseLocator

from edx_when import api, models
from test_utils import make_block_id, make_items
from tests.test_models_app.models import DummyCourse, DummyEnrollment, DummySchedule

NUM_OVERRIDES = 3


@ddt.ddt
class ApiTests(TestCase):
    """
    Tests for edx_when.api
    """
    def setUp(self):
        super(ApiTests, self).setUp()

        self.course = DummyCourse(id='course-v1:testX+tt101+2019')
        self.course.save()

        self.user = User(username='tester', email='tester@test.com')
        self.user.save()

        self.enrollment = DummyEnrollment(user=self.user, course=self.course)
        self.enrollment.save()

        self.schedule = DummySchedule(enrollment=self.enrollment, start_date=datetime(2019, 4, 1))
        self.schedule.save()

        dummy_schedule_patcher = patch('edx_when.models.Schedule', DummySchedule)
        dummy_schedule_patcher.start()
        self.addCleanup(dummy_schedule_patcher.stop)

        relative_dates_patcher = patch('edx_when.api._are_relative_dates_enabled', return_value=True)
        relative_dates_patcher.start()
        self.addCleanup(relative_dates_patcher.stop)
        self.addCleanup(cache.clear)

        cache.clear()
        DEFAULT_REQUEST_CACHE.clear()

    def test_set_dates_for_course(self):
        items = make_items()
        api.set_dates_for_course(items[0][0].course_key, items)

        cdates = models.ContentDate.objects.all()
        assert len(cdates) == NUM_OVERRIDES

    def test_get_dates_for_course(self):
        items = make_items()
        api.set_dates_for_course(items[0][0].course_key, items)
        retrieved = api.get_dates_for_course(items[0][0].course_key)
        assert len(retrieved) == NUM_OVERRIDES
        first = items[0]
        assert retrieved[(first[0], 'due')] == first[1]['due']

        # second time is cached
        retrieved = api.get_dates_for_course(items[0][0].course_key)
        assert len(retrieved) == NUM_OVERRIDES

        # third time with new course_id

        course2 = DummyCourse(id='course-v1:testX+tt202+2019')
        course2.save()
        new_items = make_items(course2.id)

        enrollment2 = DummyEnrollment(user=self.user, course=course2)
        enrollment2.save()

        schedule2 = DummySchedule(enrollment=enrollment2, start_date=datetime(2019, 4, 1))
        schedule2.save()

        api.set_dates_for_course(new_items[0][0].course_key, new_items)
        new_retrieved = api.get_dates_for_course(new_items[0][0].course_key)
        assert len(new_retrieved) == NUM_OVERRIDES
        first_id = list(new_retrieved.keys())[0][0]
        last_id = list(retrieved.keys())[0][0]
        assert first_id.course_key != last_id.course_key
        return items

    def test_get_dates_no_schedule(self):
        items = make_items(with_relative=True)
        course_key = items[0][0].course_key
        api.set_dates_for_course(course_key, items)
        retrieved = api.get_dates_for_course(course_key, user=self.user)
        assert len(retrieved) == 6
        self.schedule.delete()
        retrieved = api.get_dates_for_course(course_key, user=self.user, use_cached=False)
        assert len(retrieved) == 3

    def test_get_user_date_no_schedule(self):
        items = make_items()
        course_key = items[0][0].course_key
        api.set_dates_for_course(course_key, items)
        before_override = api.get_dates_for_course(course_key, user=self.user)
        assert len(before_override) == 3

        # Override a date for the user with a relative date, but remove the schedule
        # so that the override can't be applied
        api.set_date_for_block(course_key, items[0][0], 'due', timedelta(days=2), user=self.user)
        self.schedule.delete()

        after_override = api.get_dates_for_course(course_key, user=self.user, use_cached=False)
        assert before_override == after_override

    def test_clear_dates_for_course(self):
        items = self.test_get_dates_for_course()
        keep_date = models.ContentDate.objects.get(location=items[1][0])

        with self.assertNumQueries(1):
            api.clear_dates_for_course(items[0][0].course_key, keep=[keep_date.id])

        retrieved = api.get_dates_for_course(items[0][0].course_key, use_cached=False)
        self.assertEqual(len(retrieved), 1)
        self.assertEqual(list(retrieved.keys())[0][0], items[1][0])

        with self.assertNumQueries(1):
            api.clear_dates_for_course(items[0][0].course_key)
        self.assertEqual(api.get_dates_for_course(items[0][0].course_key, use_cached=False), {})

    def test_set_user_override_invalid_block(self):
        items = make_items()
        first = items[0]
        block_id = first[0]
        api.set_dates_for_course(six.text_type(block_id.course_key), items)

        with self.assertRaises(api.MissingDateError):
            # can't set a user override for content without a date
            bad_block_id = make_block_id()
            api.set_date_for_block(bad_block_id.course_key, bad_block_id, 'due', datetime(2019, 4, 6), user=self.user)

    @ddt.data(
        (datetime(2019, 4, 6), datetime(2019, 4, 3)),
        (datetime(2019, 4, 6), timedelta(days=-1)),
        (timedelta(days=5), timedelta(days=-1)),
    )
    @ddt.unpack
    def test_set_user_override_invalid_date(self, initial_date, override_date):
        items = make_items()
        first = items[0]
        block_id = first[0]
        items[0][1]['due'] = initial_date
        api.set_dates_for_course(six.text_type(block_id.course_key), items)

        with self.assertRaises(api.InvalidDateError):
            api.set_date_for_block(block_id.course_key, block_id, 'due', override_date, user=self.user)

    @ddt.data(
        (datetime(2019, 4, 6), datetime(2019, 4, 10), datetime(2019, 4, 10)),
        (datetime(2019, 4, 6), timedelta(days=3), datetime(2019, 4, 9)),
        (timedelta(days=3), datetime(2019, 4, 10), datetime(2019, 4, 10)),
        (timedelta(days=3), timedelta(days=2), datetime(2019, 4, 6)),
    )
    @ddt.unpack
    def test_set_user_override(self, initial_date, override_date, expected_date):
        items = make_items()
        first = items[0]
        block_id = first[0]
        items[0][1]['due'] = initial_date

        api.set_dates_for_course(six.text_type(block_id.course_key), items)

        api.set_date_for_block(block_id.course_key, block_id, 'due', override_date, user=self.user)
        DEFAULT_REQUEST_CACHE.clear()
        cache.clear()
        retrieved = api.get_dates_for_course(block_id.course_key, user=self.user.id)
        assert len(retrieved) == NUM_OVERRIDES
        assert retrieved[block_id, 'due'] == expected_date

        overrides = api.get_overrides_for_block(block_id.course_key, block_id)
        assert len(overrides) == 1
        assert overrides[0][2] == expected_date

        overrides = list(api.get_overrides_for_user(block_id.course_key, self.user))
        assert len(overrides) == 1
        assert overrides[0] == {'location': block_id, 'actual_date': expected_date}

    @ddt.data(
        (datetime(2019, 4, 6), datetime(2019, 4, 10), datetime(2019, 4, 10)),
        # The expected date shifts from 4/6 to 4/4 because once it converts to a relative date,
        # it is based off the schedule start and the users schedule start is 4/1
        (datetime(2019, 4, 6), timedelta(days=3), datetime(2019, 4, 4)),
        (timedelta(days=3), datetime(2019, 4, 10), datetime(2019, 4, 10)),
        # Because the relative date is changed for the entire course, the user's date goes
        # from 4/4 to 4/3 because it is based off the schedule start and the users
        # schedule start is 4/1. This is different from when you call set_date_for_block
        # and pass in a user as that will adjust it from the old due date (see test_set_user_override)
        (timedelta(days=3), timedelta(days=2), datetime(2019, 4, 3)),
    )
    @ddt.unpack
    def test_set_date_for_block(self, initial_date, override_date, expected_date):
        items = make_items()
        first = items[0]
        block_id = first[0]
        items[0][1]['due'] = initial_date

        api.set_dates_for_course(six.text_type(block_id.course_key), items)

        api.set_date_for_block(block_id.course_key, block_id, 'due', override_date)
        DEFAULT_REQUEST_CACHE.clear()
        cache.clear()
        retrieved = api.get_dates_for_course(block_id.course_key, user=self.user.id)
        assert len(retrieved) == NUM_OVERRIDES
        assert retrieved[block_id, 'due'] == expected_date

    @ddt.data(
        (datetime(2019, 4, 6), datetime(2019, 4, 10), datetime(2019, 4, 10)),
        (datetime(2019, 4, 6), timedelta(days=3), datetime(2019, 4, 9)),
        (timedelta(days=3), datetime(2019, 4, 10), datetime(2019, 4, 10)),
        (timedelta(days=3), timedelta(days=2), datetime(2019, 4, 6)),
    )
    @ddt.unpack
    def test_remove_user_override(self, initial_date, override_date, expected_date):
        items = make_items()
        first = items[0]
        block_id = first[0]
        items[0][1]['due'] = initial_date

        api.set_dates_for_course(six.text_type(block_id.course_key), items)

        api.set_date_for_block(block_id.course_key, block_id, 'due', override_date, user=self.user)
        DEFAULT_REQUEST_CACHE.clear()
        cache.clear()
        retrieved = api.get_dates_for_course(block_id.course_key, user=self.user.id)
        assert len(retrieved) == NUM_OVERRIDES
        assert retrieved[block_id, 'due'] == expected_date

        api.set_date_for_block(block_id.course_key, block_id, 'due', None, user=self.user)
        DEFAULT_REQUEST_CACHE.clear()
        cache.clear()
        retrieved = api.get_dates_for_course(block_id.course_key, user=self.user.id)
        assert len(retrieved) == NUM_OVERRIDES
        if isinstance(initial_date, timedelta):
            user_initial_date = self.schedule.start_date + initial_date
        else:
            user_initial_date = initial_date
        assert retrieved[block_id, 'due'] == user_initial_date

    def test_get_date_for_block(self):
        items = make_items()
        course_id = items[0][0].course_key
        api.set_dates_for_course(course_id, items)
        block_id, data = items[0]
        assert api.get_date_for_block(course_id, block_id, user=self.user) == data['due']
        assert api.get_date_for_block(course_id, 'bad', user=self.user) is None

    def test_is_enabled(self):
        items = make_items()
        course_id = items[0][0].course_key
        assert not api.is_enabled_for_course(course_id)
        api.set_dates_for_course(course_id, items)
        assert api.is_enabled_for_course(course_id)

    def test_allow_relative_dates(self):
        course_key = CourseLocator('testX', 'tt101', '2019')
        block1 = make_block_id(course_key)
        date1 = datetime(2019, 3, 22)
        block2 = make_block_id(course_key)
        date2 = datetime(2019, 3, 23)
        date2_override_delta = timedelta(days=10)
        date2_override = date2 + date2_override_delta
        block3 = make_block_id(course_key)
        date3_delta = timedelta(days=1)
        date3 = self.schedule.start_date + date3_delta
        block4 = make_block_id(course_key)
        date4_delta = timedelta(days=2)
        date4 = self.schedule.start_date + date4_delta
        date4_override = datetime(2019, 4, 24)
        items = [
            (block1, {'due': date1}),  # absolute
            (block2, {'due': date2}),  # absolute, to be overwritten by relative date
            (block3, {'due': date3_delta}),  # relative
            (block4, {'due': date4_delta}),  # relative, to be overwritten by absolute date
        ]
        api.set_dates_for_course(course_key, items)
        api.set_date_for_block(course_key, block2, 'due', date2_override_delta, user=self.user)
        api.set_date_for_block(course_key, block4, 'due', date4_override, user=self.user)

        # get_dates_for_course
        dates = [
            ((block1, 'due'), date1),
            ((block2, 'due'), date2),
            ((block3, 'due'), date3),
            ((block4, 'due'), date4),
        ]
        user_dates = [
            ((block1, 'due'), date1),
            ((block2, 'due'), date2_override),
        ]
        assert api.get_dates_for_course(course_key, schedule=self.schedule) == dict(dates)
        with patch('edx_when.api._are_relative_dates_enabled', return_value=False):
            assert api.get_dates_for_course(course_key, schedule=self.schedule) == dict(dates[0:2])
            assert api.get_dates_for_course(course_key, schedule=self.schedule, user=self.user) == dict(user_dates)

        # get_date_for_block
        assert api.get_date_for_block(course_key, block2) == date2
        assert api.get_date_for_block(course_key, block4, user=self.user) == date4_override
        with patch('edx_when.api._are_relative_dates_enabled', return_value=False):
            assert api.get_date_for_block(course_key, block2) == date2
            assert api.get_date_for_block(course_key, block1, user=self.user) == date1
            assert api.get_date_for_block(course_key, block2, user=self.user) == date2_override
            assert api.get_date_for_block(course_key, block4, user=self.user) is None

        # get_overrides_for_block
        block2_overrides = [(self.user.username, 'unknown', date2_override)]
        assert api.get_overrides_for_block(course_key, block2) == block2_overrides
        with patch('edx_when.api._are_relative_dates_enabled', return_value=False):
            assert api.get_overrides_for_block(course_key, block2) == [(self.user.username, 'unknown', date2_override)]

        # get_overrides_for_user
        user_overrides = [
            {'location': block4, 'actual_date': date4_override},
            {'location': block2, 'actual_date': date2_override},
        ]
        assert list(api.get_overrides_for_user(course_key, self.user)) == user_overrides
        with patch('edx_when.api._are_relative_dates_enabled', return_value=False):
            assert list(api.get_overrides_for_user(course_key, self.user)) == user_overrides

    def test_relative_date_past_end_date(self):
        course_key = CourseLocator('testX', 'tt101', '2019')
        start_block = make_block_id(course_key, block_type='course')
        start_date = datetime(2019, 3, 15)
        before_end_date_block = make_block_id(course_key)
        before_end_date_delta = timedelta(days=1)
        before_end_date = self.schedule.start_date + before_end_date_delta
        after_end_date_block = make_block_id(course_key)
        after_end_date_delta = timedelta(days=10)
        end_block = make_block_id(course_key, block_type='course')
        end_date = datetime(2019, 4, 4)
        items = [
            (start_block, {'start': start_date}),  # start dates are always absolute
            (before_end_date_block, {'due': before_end_date_delta}),  # relative
            (after_end_date_block, {'due': after_end_date_delta}),  # relative
            (end_block, {'end': end_date}),  # end dates are always absolute
        ]
        api.set_dates_for_course(course_key, items)

        dates = [
            ((start_block, 'start'), start_date),
            ((before_end_date_block, 'due'), before_end_date),
            # Because the end date for this block would have been after the course end date,
            # the block will have an end date of the course end date
            ((after_end_date_block, 'due'), end_date),
            ((end_block, 'end'), end_date),
        ]
        assert api.get_dates_for_course(course_key, schedule=self.schedule) == dict(dates)

    @ddt.data(*[
        (has_schedule, pass_user_object, pass_schedule, item_count)
        for has_schedule in (True, False)
        for pass_user_object in (True, False)
        for pass_schedule in (True, False)
        for item_count in (1, 5, 25, 100)
    ])
    @ddt.unpack
    def test_get_dates_for_course_query_counts(self, has_schedule, pass_user_object, pass_schedule, item_count):
        if not has_schedule:
            self.schedule.delete()
        items = [
            (make_block_id(self.course.id), {'due': datetime(2020, 1, 1) + timedelta(days=i)})
            for i in range(item_count)
        ]
        api.set_dates_for_course(self.course.id, items)

        user = self.user if pass_user_object else self.user.id
        schedule = self.schedule if pass_schedule and has_schedule else None

        if has_schedule and pass_schedule:
            query_count = 2
        else:
            query_count = 3
        with self.assertNumQueries(query_count):
            dates = api.get_dates_for_course(
                course_id=self.course.id, user=user, schedule=schedule
            )

        # Second time, the request cache eliminates all querying...
        with self.assertNumQueries(0):
            cached_dates = api.get_dates_for_course(
                course_id=self.course.id, user=user, schedule=schedule
            )
            assert dates == cached_dates

        # Now wipe the request cache...
        DEFAULT_REQUEST_CACHE.clear()

        # This time, test the external cache (which eliminates the one large
        # query to ContentDates).
        with self.assertNumQueries(query_count - 1):
            externally_cached_dates = api.get_dates_for_course(
                course_id=self.course.id, user=user, schedule=schedule
            )
            assert dates == externally_cached_dates

        # Finally, force uncached behavior with used_cache=False
        with self.assertNumQueries(query_count):
            uncached_dates = api.get_dates_for_course(
                course_id=self.course.id, user=user, schedule=schedule, use_cached=False
            )
            assert dates == uncached_dates

    def test_set_dates_for_course_query_counts(self):
        items = make_items()

        with self.assertNumQueries(2):  # two for transaction wrappers
            with patch('edx_when.api.set_date_for_block', return_value=1) as mock_set:
                with patch('edx_when.api.clear_dates_for_course') as mock_clear:
                    api.set_dates_for_course(self.course.id, items)

        self.assertEqual(mock_set.call_count, NUM_OVERRIDES)
        self.assertEqual(mock_clear.call_args_list, [call(self.course.id, keep=[1] * NUM_OVERRIDES)])

    def test_set_date_for_block_query_counts(self):
        args = (self.course.id, make_block_id(self.course.id), 'due', datetime(2019, 3, 22))

        # Each date we make has:
        #  1 get & 1 create for the date itself
        #  1 get & 1 create for the sub-policy (plus 2 for starting/stopping transactions)
        with self.assertNumQueries(2 + 4):
            api.set_date_for_block(*args)

        # When setting same items, we should only do initial read
        with self.assertNumQueries(1):
            api.set_date_for_block(*args)


class ApiWaffleTests(TestCase):
    """
    Tests for edx_when.api waffle usage.

    These are isolated because they have pretty different patch requirements.
    """
    @patch.dict(sys.modules, {'openedx.features.course_experience': Mock()})
    def test_relative_dates_enabled(self):
        from openedx.features.course_experience import RELATIVE_DATES_FLAG as mock_flag  # pylint: disable=import-error
        mock_flag.is_enabled.return_value = True
        assert api._are_relative_dates_enabled()  # pylint: disable=protected-access
        assert mock_flag.is_enabled.called

    @patch.dict(sys.modules, {'openedx.features.course_experience': Mock()})
    def test_relative_dates_disabled(self):
        from openedx.features.course_experience import RELATIVE_DATES_FLAG as mock_flag  # pylint: disable=import-error
        mock_flag.is_enabled.return_value = False
        assert not api._are_relative_dates_enabled()  # pylint: disable=protected-access
        assert mock_flag.is_enabled.called

    @patch.dict(sys.modules, {'openedx.features.course_experience': None})
    def test_relative_dates_import_error(self):
        assert not api._are_relative_dates_enabled()  # pylint: disable=protected-access
