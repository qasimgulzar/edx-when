"""
API for retrieving and setting dates.
"""
from __future__ import absolute_import, unicode_literals

import logging

from edx_django_utils.cache.utils import DEFAULT_REQUEST_CACHE
from opaque_keys.edx.keys import CourseKey, UsageKey

from edx_when import models

log = logging.getLogger('edx-when')

FIELDS_TO_EXTRACT = ('due', 'start')


def _ensure_key(key_class, key_obj):
    if not isinstance(key_obj, key_class):
        key_obj = key_class.from_string(key_obj)
    return key_obj


def set_dates_for_course(course_key, items):
    """
    Extract dates from blocks.

    items is an iterator of (location, field metadata dictionary)
    """
    course_key = _ensure_key(CourseKey, course_key)
    models.ContentDate.objects.filter(course_id=course_key, active=True).update(active=False)
    for location, fields in items:
        for field in FIELDS_TO_EXTRACT:
            if field in fields:
                val = fields[field]
                if val:
                    log.info('Setting date for %r, %s, %r', location, field, val)
                    set_date_for_block(course_key, location, field, val)


def get_dates_for_course(course_id, user=None):
    """
    Return dictionary of dates for the given course_id and optional user.

        key: block location, field name
        value: datetime object
    """
    cache_key = 'course_dates.%s.%s' % (course_id, user.id if user else '')
    dates = DEFAULT_REQUEST_CACHE.data.get(cache_key, None)
    if dates is not None:
        return dates
    course_id = _ensure_key(CourseKey, course_id)
    qset = models.ContentDate.objects.filter(course_id=course_id, active=True).select_related('policy')
    dates = {}
    policies = {}
    for cdate in qset:
        key = (cdate.location, cdate.field)
        dates[key] = cdate.policy.abs_date
        policies[cdate.id] = key
    if user:
        for userdate in models.UserDate.objects.filter(
                user=user,
                content_date__course_id=course_id,
                content_date__active=True).select_related(
                    'content_date', 'content_date__policy'
                ).order_by('modified'):
            dates[policies[userdate.content_date_id]] = userdate.actual_date
    DEFAULT_REQUEST_CACHE.data[cache_key] = dates
    return dates


def get_overrides_for_block(course_id, block_id):
    """
    Return list of date overrides for a block.

    list of (username, full_name, date)
    """
    course_id = _ensure_key(CourseKey, course_id)
    block_id = _ensure_key(UsageKey, block_id)

    query = models.UserDate.objects.filter(
                content_date__course_id=course_id,
                content_date__location=block_id,
                content_date__active=True).order_by('-modified')
    dates = []
    users = set()
    for udate in query:
        if udate.user_id in users:
            continue
        else:
            users.add(udate.user_id)
        username = udate.user.username
        full_name = udate.user.profile.name
        override = udate.actual_date
        dates.append((username, full_name, override))
    return dates


def get_overrides_for_user(course_id, user):
    """
    Return all user date overrides for a particular course.

    iterator of {'location': location, 'actual_date': date}
    """
    course_id = _ensure_key(CourseKey, course_id)

    query = models.UserDate.objects.filter(
                content_date__course_id=course_id,
                user=user,
                content_date__active=True).order_by('-modified')
    users = set()
    for udate in query:
        if udate.user_id in users:
            continue
        else:
            users.add(udate.user_id)
        yield {'location': udate.content_date.location, 'actual_date': udate.actual_date}


def set_date_for_block(course_id, block_id, field, abs_date, rel_date=None, user=None, reason='', actor=None):
    """
    Save the date for a particular field in a block.

    abs_date: datetime object
    rel_date: a relative date integer (in days?)
    user: user object to override date
    reason: explanation for override
    actor: user object of person making the override
    """
    course_id = _ensure_key(CourseKey, course_id)
    block_id = _ensure_key(UsageKey, block_id)
    try:
        existing_date = models.ContentDate.objects.get(course_id=course_id, location=block_id, field=field)
        existing_date.active = True
    except models.ContentDate.DoesNotExist:
        if user:
            raise MissingDateError(block_id)
        existing_date = models.ContentDate(course_id=course_id, location=block_id, field=field)
        existing_date.policy, __ = models.DatePolicy.objects.get_or_create(abs_date=abs_date)

    if user:
        userd = models.UserDate(user=user, abs_date=abs_date, rel_date=rel_date)
        userd.actor = actor
        userd.reason = reason or ''
        userd.content_date = existing_date
        if userd.actual_date < existing_date.policy.abs_date:
            raise InvalidDateError(userd.actual_date)
        userd.save()
        log.info('Saved override for user=%d loc=%s date=%s', userd.user_id, userd.location, userd.actual_date)
    else:
        if existing_date.policy.abs_date != abs_date:
            log.info('updating policy %r', existing_date)
            existing_date.policy = models.DatePolicy.objects.get_or_create(abs_date=abs_date)[0]
    existing_date.save()


class BaseWhenException(Exception):
    pass


class MissingDateError(BaseWhenException):
    pass


class InvalidDateError(BaseWhenException):
    pass
