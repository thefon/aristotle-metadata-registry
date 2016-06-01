from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import PermissionDenied, ImproperlyConfigured
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.urlresolvers import reverse
from django.db import transaction
from django.forms.models import modelformset_factory
from django.http import HttpResponse, Http404, HttpResponseRedirect
from django.shortcuts import render, redirect, get_object_or_404
from django.template import RequestContext, TemplateDoesNotExist
from django.template.defaultfilters import slugify
from django.template.loader import select_template
from django.utils.translation import ugettext_lazy as _
from django.views.generic import TemplateView
from django.utils import timezone
from django.utils.decorators import method_decorator
import datetime

import reversion
from reversion.revisions import default_revision_manager
from reversion_compare.views import HistoryCompareDetailView

from aristotle_mdr.perms import user_can_view, user_can_edit, user_can_change_status
from aristotle_mdr import perms
from aristotle_mdr.utils import cache_per_item_user, concept_to_dict, construct_change_message, url_slugify_concept
from aristotle_mdr import forms as MDRForms
from aristotle_mdr import models as MDR
from aristotle_mdr.views import get_if_user_can_view

import logging

logger = logging.getLogger(__name__)
logger.debug("Logging started for " + __name__)

PAGES_PER_RELATED_ITEM = 15


def download(request, download_type, iid=None):
    """
    By default, ``aristotle_mdr.views.download`` is called whenever a URL matches
    the pattern defined in ``aristotle_mdr.urls_aristotle``::

        download/(?P<download_type>[a-zA-Z0-9\-\.]+)/(?P<iid>\d+)/?

    This is passed into ``download`` which resolves the item id (``iid``), and
    determins if a user has permission to view the request item with that id. If
    a user is allowed to download this file, ``download`` iterates through each
    download type defined in ``ARISTOTLE_DOWNLOADS``.

    A download option tuple takes the following form form::

        ('file_type','display_name','font_awesome_icon_name','module_name'),

    With ``file_type`` allowing only ASCII alphanumeric and underscores,
    ``display_name`` can be any valid python string,
    ``font_awesome_icon_name`` can be any Font Awesome icon and
    ``module_name`` is the name of the python module that provides a downloader
    for this file type.

    For example, included with Aristotle-MDR is a PDF downloader which has the
    download definition tuple::

            ('pdf','PDF','fa-file-pdf-o','aristotle_mdr'),

    Where a ``file_type`` multiple is defined multiple times, **the last matching
    instance in the tuple is used**.

    Next, the module that is defined for a ``file_type`` is dynamically imported using
    ``exec``, and is wrapped in a ``try: except`` block to catch any exceptions. If
    the ``module_name`` does not match the regex ``^[a-zA-Z0-9\_]+$`` ``download``
    raises an exception.

    If the module is able to be imported, ``downloader.py`` from the given module
    is imported, this file **MUST** have a ``download`` function defined which returns
    a Django ``HttpResponse`` object of some form.
    """
    item = MDR._concept.objects.get_subclass(pk=iid)
    item = get_if_user_can_view(item.__class__, request.user, iid)
    if not item:
        if request.user.is_anonymous():
            return redirect(reverse('friendly_login') + '?next=%s' % request.path)
        else:
            raise PermissionDenied

    downloadOpts = getattr(settings, 'ARISTOTLE_DOWNLOADS', "")
    module_name = ""
    for d in downloadOpts:
        dt = d[0]
        if dt == download_type:
            module_name = d[3]
    if module_name:
        import re
        if not re.search('^[a-zA-Z0-9\-\.]+$', download_type):  # pragma: no cover
            # Invalid download_type
            raise registry_exceptions.BadDownloadTypeAbbreviation("Download type can only be composed of letters, numbers, hyphens or periods.")
        elif not re.search('^[a-zA-Z0-9\_]+$', module_name):  # pragma: no cover
            # bad module_name
            raise registry_exceptions.BadDownloadModuleName("Download name isn't a valid Python module name.")
        try:
            downloader = None
            # dangerous - we are really trusting the settings creators here.
            exec("import %s.downloader as downloader" % module_name)
            return downloader.download(request, download_type, item)
        except TemplateDoesNotExist:
            debug = getattr(settings, 'DEBUG')
            if debug:
                raise
            raise Http404

    raise Http404


def bulk_download(request, download_type, items=None):
    """
    By default, ``aristotle_mdr.views.download`` is called whenever a URL matches
    the pattern defined in ``aristotle_mdr.urls_aristotle``::

        download/(?P<download_type>[a-zA-Z0-9\-\.]+)/(?P<iid>\d+)/?

    This is passed into ``download`` which resolves the item id (``iid``), and
    determins if a user has permission to view the request item with that id. If
    a user is allowed to download this file, ``download`` iterates through each
    download type defined in ``ARISTOTLE_DOWNLOADS``.

    A download option tuple takes the following form form::

        ('file_type','display_name','font_awesome_icon_name','module_name'),

    With ``file_type`` allowing only ASCII alphanumeric and underscores,
    ``display_name`` can be any valid python string,
    ``font_awesome_icon_name`` can be any Font Awesome icon and
    ``module_name`` is the name of the python module that provides a downloader
    for this file type.

    For example, included with Aristotle-MDR is a PDF downloader which has the
    download definition tuple::

            ('pdf','PDF','fa-file-pdf-o','aristotle_mdr'),

    Where a ``file_type`` multiple is defined multiple times, **the last matching
    instance in the tuple is used**.

    Next, the module that is defined for a ``file_type`` is dynamically imported using
    ``exec``, and is wrapped in a ``try: except`` block to catch any exceptions. If
    the ``module_name`` does not match the regex ``^[a-zA-Z0-9\_]+$`` ``download``
    raises an exception.

    If the module is able to be imported, ``downloader.py`` from the given module
    is imported, this file **MUST** have a ``download`` function defined which returns
    a Django ``HttpResponse`` object of some form.
    """
    items=[]

    for iid in request.GET.getlist('items'):
        item = MDR._concept.objects.get_subclass(pk=iid)
        item = get_if_user_can_view(item.__class__, request.user, iid)
        items.append(item)

    downloadOpts = getattr(settings, 'ARISTOTLE_DOWNLOADS', "")
    module_name = ""
    for d in downloadOpts:
        dt = d[0]
        if dt == download_type:
            module_name = d[3]
    if module_name:
        import re
        if not re.search('^[a-zA-Z0-9\-\.]+$', download_type):  # pragma: no cover
            # Invalid download_type
            raise registry_exceptions.BadDownloadTypeAbbreviation("Download type can only be composed of letters, numbers, hyphens or periods.")
        elif not re.search('^[a-zA-Z0-9\_]+$', module_name):  # pragma: no cover
            # bad module_name
            raise registry_exceptions.BadDownloadModuleName("Download name isn't a valid Python module name.")
        try:
            downloader = None
            # dangerous - we are really trusting the settings creators here.
            exec("import %s.downloader as downloader" % module_name)
            return downloader.bulk_download(request, download_type, items)
        except TemplateDoesNotExist:
            debug = getattr(settings, 'DEBUG')
            if debug:
                raise
            raise Http404

    raise Http404
