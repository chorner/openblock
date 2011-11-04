#   Copyright 2011 OpenPlans, and contributors
#
#   This file is part of ebpub
#
#   ebpub is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   ebpub is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with ebpub.  If not, see <http://www.gnu.org/licenses/>.
#

from django.conf import settings
from django.contrib.gis import geos
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.views.decorators.csrf import csrf_protect
from ebpub.accounts.models import User
from ebpub.accounts.utils import login_required
from ebpub.db.models import Schema, NewsItem, SchemaField, Lookup
from ebpub.neighbornews.forms import NeighborMessageForm, NeighborEventForm
from ebpub.neighbornews.models import NewsItemCreator
from ebpub.neighbornews.utils import NEIGHBOR_MESSAGE_SLUG, NEIGHBOR_EVENT_SLUG
from ebpub.neighbornews.utils import if_disabled404
from ebpub.utils.view_utils import eb_render
import datetime
import re

@if_disabled404(NEIGHBOR_MESSAGE_SLUG)
@login_required
@csrf_protect
def new_message(request):
    schema = Schema.objects.get(slug=NEIGHBOR_MESSAGE_SLUG)
    FormType = NeighborMessageForm
    return _new_item(request, schema, FormType)

@if_disabled404(NEIGHBOR_EVENT_SLUG)
@login_required
@csrf_protect
def new_event(request):
    schema = Schema.objects.get(slug=NEIGHBOR_EVENT_SLUG)
    FormType = NeighborEventForm
    return _new_item(request, schema, FormType)


# @if_disabled404(NEIGHBOR_EVENT_SLUG)
# @login_required
# @csrf_protect
# def edit_event(request):
#     # XXX Get the existing event, validate was created by this user.
#     schema = Schema.objects.get(slug=NEIGHBOR_EVENT_SLUG)
#     form = NeighborEventForm(
#     return _new_item(request, schema, FormType, _create_event)



################################################################
# Utility functions.

def _new_item(request, schema, FormType):
    if request.method == 'POST':
        form = FormType(request.POST)
        if form.is_valid():
            form.instance.schema = schema
            item = form.save()

            # Add a NewsItemCreator association; un-lazy the User.
            import pdb; pdb.set_trace()
            user = User.objects.get(id=request.user.id)
            creator = NewsItemCreator(news_item=item, user=user)
            creator.save()

            # Image link.
            if form.cleaned_data['image_url']:
                item.attributes['image_url'] = form.cleaned_data['image_url']

            # 'categories'
            cats = [cat for cat in form.cleaned_data['categories'].split(',') if cat.strip()]
            if len(cats):
                cat_field = SchemaField.objects.get(schema=schema, name='categories')
                lookups = set()
                for cat in cats:
                    code = _category_code(cat)
                    nice_name = _category_nice_name(cat)
                    lu = Lookup.objects.get_or_create_lookup(cat_field, nice_name, code, "", False)
                    lookups.add(lu.id)
                item.attributes['categories'] = ','.join(['%d' % luid for luid in lookups])

            detail_url = reverse('ebpub-newsitem-detail',
                                 args=(schema.slug, '%d' % item.id))
            return HttpResponseRedirect(detail_url)
    else:
        form = FormType()

    mapconfig = {
        'locations': [],
        'layers': [],
        'baselayer_type': settings.MAP_BASELAYER_TYPE,

    }
    ctx = {
        'form': form,
        'map_configuration': mapconfig,
        'default_lon': settings.DEFAULT_MAP_CENTER_LON,
        'default_lat': settings.DEFAULT_MAP_CENTER_LAT,
        'default_zoom': settings.DEFAULT_MAP_ZOOM,
        'schema': schema
    }
    return eb_render(request, "neighbornews/new_message.html", ctx)


def _category_code(cat):
    code = cat
    code = code.strip()
    code = code.lower()
    code = re.sub('\s+', ' ', code)
    code = re.sub('[^\w]', '-', code)
    return code

def _category_nice_name(cat):
    nice = cat
    nice = nice.strip()
    nice = re.sub('\s+', ' ', nice)
    nice = nice.lower()
    return nice

def news_by_user(request, userid):
    user = User.objects.get(id=userid)
    is_viewing_self = False
    if not request.user.is_anonymous():
        if user.id == request.user.id:
            is_viewing_self = True
    items_by_schema = []
    for slug in ('neighbor-messages', 'neighbor-events'):
        try:
            schema = Schema.objects.get(slug=slug)
        except Schema.DoesNotExist:
            continue
        items = NewsItemCreator.objects.filter(user__id=userid, news_item__schema=schema)
        items = items.select_related().order_by('-news_item__item_date')
        items = [item.news_item for item in items]
        items_by_schema.append({'schema': schema, 'items': items})

    context = {'items_by_schema': items_by_schema, 'user': user,
               'is_viewing_self': is_viewing_self}
    return eb_render(request, "neighbornews/news_by_user.html", context)
