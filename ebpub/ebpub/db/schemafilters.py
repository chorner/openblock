"""
Use cases:

  1. Generate a chain of filters, given view arguments and/or query
     string.  - DONE

  2. Generate reverse urls, complete with query string (if we use them),
     given one or more filters.

     a. For schema_filter() html view - DONE

     b. For REST API views  - TODO


Chain of filters needs to support:

  1. Add a filter to the chain, by name. - DONE

  2. Remove a filter from the chain, by name  - DONE

  3. get a filter from the chain, by name - DONE

  4. raise Http404() if conflicting filters are added (eg. two date
     filters) - or raise a custom exception which wrapper views can
     handle however they like. - DONE

  5. OPTIMIZATION: normalize the order of filters by increasing
     expense - DONE

     TODO: ... except for the hard part of testing which order
     is actualy optimal.  Currently guessing it to be: schema, date,
     non-lookup attrs, block/location, lookup attrs, text search.
     This will need profiling with lots of test data.

  6. SEO and CACHEABILITY: Redirect to a normalized form of the URL
     for better cacheability. - DONE but needs refactoring

  7. copy() a filter chain - useful for making mutated variations,
     which could be used with our reverse() to create "remove
     this filter" links in the UI. - DONE

  8. get a list of breadcrumb links for the whole chain.

     DONE

"""


from django.utils import dateformat
from django.utils.datastructures import SortedDict
from ebpub.db import constants
from ebpub.db import models
from ebpub.db.utils import block_radius_value
from ebpub.db.utils import make_search_buffer
from ebpub.db.utils import url_to_block
from ebpub.db.utils import url_to_location
from ebpub.metros.allmetros import get_metro
from ebpub.utils.view_utils import parse_pid
from ebpub.utils.view_utils import radius_from_urlfragment
from ebpub.utils.view_utils import radius_url
from ebpub.utils.view_utils import radius_urlfragment

import calendar
import datetime
import ebpub.streets.models
import logging
import re
import urllib

logger = logging.getLogger('ebpub.db.schemafilters')

class NewsitemFilter(object):

    _sort_value = 100.0

    # Various attributes used for URL construction, breadcrumbs,
    # and other UI stuff.
    # If these are None, they should not be shown to the user.
    name = None
    argname = None
    url = None
    value = None
    label = None
    short_value = None

    def __init__(self, request, context, queryset=None, *args, **kw):
        self.qs = queryset if (queryset is not None) else models.NewsItem.objects.all()
        self.context = context
        self.request = request
        self._got_args = False

    def apply(self):
        """mutate the queryset, and any other state that needs sharing
        with others.
        """
        raise NotImplementedError # pragma: no cover

    def validate(self):
        """
        If we didn't get enough info from the args, eg. it's a
        Location filter but no location was specified, then return a
        dict of stuff for putting in a template context.

        ... or maybe should be something more generic across both REST
        and UI views
        """
        # TODO: Maybe split this into .get_extra_context() -> dict
        # and .needs_more_info() -> bool
        raise NotImplementedError  # pragma: no cover

    def get(self, key, default=None):
        # Emulate a dict to make legacy code in ebpub.db.breadcrumbs happy
        return getattr(self, key, default)

    def __getitem__(self, key):
        # Emulate a dict to make legacy code in ebpub.db.breadcrumbs happy
        default = object()
        result = getattr(self, key, default)
        if result is default:
            raise KeyError(key)
        return result


class FilterError(Exception):
    def __init__(self, msg, url=None):
        self.msg = msg
        self.url = url

    def __str__(self):
        return repr(self.msg)


class SchemaFilter(NewsitemFilter):

    _sort_value = -9999
    name = 'schema'
    url = None

    def __init__(self, request, context, queryset, *args, **kwargs):
        NewsitemFilter.__init__(self, request, context, queryset, *args, **kwargs)
        self.schema = kwargs['schema']

    def validate(self):
        return {}

    def apply(self):
        self.qs = self.qs.filter(schema=self.schema)



class AttributeFilter(NewsitemFilter):

    """base class for more specific types of attribute filters
    (LookupFilter, TextSearchFilter, etc).
    """

    _sort_value = 101.0

    def __init__(self, request, context, queryset, *args, **kwargs):
        NewsitemFilter.__init__(self, request, context, queryset, *args, **kwargs)
        self.schemafield = kwargs['schemafield']
        self.name = self.schemafield.name
        self.argname = 'by-%s' % self.schemafield.name
        self.url = 'by-%s=' % self.schemafield.slug
        self.value = self.short_value = ''
        self.label = self.schemafield.pretty_name
        if args:
            # This should work for int and varchar fields. (TODO: UNTESTED)
            self.att_value = args[0]
            self._got_args = True
            if isinstance(self.att_value, datetime.date):
                str_att_value = self.att_value.strftime('%Y-%m-%d')
            elif isinstance(self.att_value, datetime.time):
                str_att_value = self.att_value.strftime('%H:%M:%S')
            elif isinstance(self.att_value, datetime.datetime):
                # Zone??
                str_att_value = self.att_value.strftime('%Y-%m-%dT%H:%M:%S')
            else:
                str_att_value = str(self.att_value)
            self.url += str_att_value

    def apply(self):
        self.qs = self.qs.by_attribute(self.schemafield, self.att_value)


class TextSearchFilter(AttributeFilter):

    """Does a text search on values of the given attribute.
    """

    _sort_value = 1000.0

    def __init__(self, request, context, queryset, *args, **kwargs):
        AttributeFilter.__init__(self, request, context, queryset, *args, **kwargs)
        self.label = self.schemafield.pretty_name
        if not args:
            raise FilterError('Text search lookup requires search params')
        self.query = ', '.join(args)
        self.short_value = self.query
        self.value = self.query
        self.url = 'by-%s=%s' % (self.schemafield.slug, self.query)

    def apply(self):
        self.qs = self.qs.text_search(self.schemafield, self.query)

    def validate(self):
        return {}

class BoolFilter(AttributeFilter):

    _sort_value = 100.0

    def __init__(self, request, context, queryset, *args, **kwargs):
        AttributeFilter.__init__(self, request, context, queryset, *args, **kwargs)
        if len(args) > 1:
            raise FilterError("Invalid boolean arg %r" % ','.join(args))
        elif len(args) == 1:
            self.boolslug = args[0]
            self.real_val = {'yes': True, 'no': False, 'na': None}.get(self.boolslug, self.boolslug)
            if self.real_val not in (True, False, None):
                raise FilterError('Invalid boolean value %r' % self.boolslug)
            self.url = 'by-%s=%s' % (self.schemafield.slug, self.boolslug)
            self._got_args = True
        else:
            # No args.
            self.value = u'By whether they %s' % self.schemafield.pretty_name_plural
            self._got_args = False

    def validate(self):
        if self._got_args:
            return {}
        return {
            'filter_argname': self.argname,
            'lookup_type': self.value[3:],
            'lookup_type_slug': self.schemafield.slug,
            'lookup_list': [{'slug': 'yes', 'name': 'Yes'}, {'slug': 'no', 'name': 'No'}, {'slug': 'na', 'name': 'N/A'}],
            }


    def apply(self):
        self.qs = self.qs.by_attribute(self.schemafield, self.real_val)
        self.short_value = {True: 'Yes', False: 'No', None: 'N/A'}[self.real_val]
        self.value = u'%s%s: %s' % (self.label[0].upper(), self.label[1:], self.short_value)


class LookupFilter(AttributeFilter):

    _sort_value = 900.0

    def __init__(self, request, context, queryset, *args, **kwargs):
        AttributeFilter.__init__(self, request, context, queryset, *args, **kwargs)
        try:
            slug = args[0]
            self._got_args = True
        except IndexError:
            self._got_args = False
            self.look = None
        if self._got_args:
            if isinstance(slug, models.Lookup):
                self.look = slug
                slug = self.look.slug
            else:
                try:
                    self.look = models.Lookup.objects.get(
                        schema_field__id=self.schemafield.id, slug=slug)
                except models.Lookup.DoesNotExist:
                    raise FilterError("No such lookup %r" % slug)
            self.value = self.look.name
            self.short_value = self.value
            self.url = 'by-%s=%s' % (self.schemafield.slug, slug)

    def validate(self):
        if self._got_args:
            return {}
        lookup_list = models.Lookup.objects.filter(schema_field__id=self.schemafield.id).order_by('name')
        return {
            'lookup_type': self.schemafield.pretty_name,
            'lookup_type_slug': self.schemafield.slug,
            'filter_argname': self.argname,
            'lookup_list': lookup_list,
        }

    def apply(self):
        self.qs = self.qs.by_attribute(self.schemafield, self.look, is_lookup=True)


class LocationFilter(NewsitemFilter):

    _sort_value = 200.0

    name = 'location'  # XXX deprecate this? used by eb_filter template tags
    argname = 'locations'

    def __init__(self, request, context, queryset, *args, **kwargs):
        NewsitemFilter.__init__(self, request, context, queryset, *args, **kwargs)
        self.location_object = None
        if 'location' in kwargs:
            self._update_location(kwargs['location'])
            self._got_args = True
        else:
            if 'location_type' in kwargs:
                self.location_type = kwargs['location_type']
                self.location_type_slug = self.location_type.slug
            else:
                if not args:
                    raise FilterError("not enough args")
                self.location_type_slug = args[0]
            self.url = 'locations=%s' % self.location_type_slug
            self.value = 'Choose %s' % self.location_type_slug.title()
            try:
                self.location_slug = args[1]
                self._got_args = True
            except IndexError:
                self._got_args = False

    def _update_location(self, loc):
        self.location_slug = loc.slug
        self.location_type = loc.location_type
        self.location_type_slug = loc.location_type.slug
        self.label = loc.location_type.name
        self.short_value = loc.name
        self.value = loc.name
        self.url = 'locations=%s,%s' % (self.location_type_slug, self.location_slug)
        self.location_name = loc.name
        self.location_object = loc

    def validate(self):
        # List of available locations for this location type.
        if self._got_args:
            return {}
        else:
            lookup_list = models.Location.objects.filter(location_type__slug=self.location_type_slug, is_public=True).order_by('display_order')
            if not lookup_list:
                raise FilterError("empty lookup list")
            location_type = lookup_list[0].location_type
            return {
                'lookup_type': location_type.name,
                'lookup_type_slug': self.location_type_slug,
                'lookup_list': lookup_list,
                'filter_argname': self.argname,
                }


    def apply(self):
        """
        filtering by Location
        """
        if self.location_object is not None:
            loc = self.location_object
        else:
            loc = url_to_location(self.location_type_slug, self.location_slug)
        self.qs = self.qs.filter(newsitemlocation__location__id=loc.id)
        self._update_location(loc)


class BlockFilter(NewsitemFilter):

    name = 'location'

    _sort_value = 200.0

    def _update_block(self, block):
        self.location_object = self.context['place'] = block
        self.city_slug = block.city  # XXX is that a slug?
        self.street_slug = block.street_slug
        self.block_range = block.number() + block.dir_url_bit()
        self.label = 'Area'
        # Assume we already have self.block_radius.
        value = '%s block%s around %s' % (self.block_radius, (self.block_radius != '1' and 's' or ''), block.pretty_name)
        self.short_value = value
        self.value = value
        self.url = 'streets=%s,%s,%s' % (block.street_slug,
                                         '%d-%d' % (block.from_num, block.to_num),
                                         radius_urlfragment(self.block_radius))
        self.location_name = block.pretty_name


    def __init__(self, request, context, queryset, *args, **kwargs):
        NewsitemFilter.__init__(self, request, context, queryset, *args, **kwargs)
        self.location_object = None
        args = list(args)

        if 'block' not in kwargs:
            # We do this first so we consume the right number of args
            # before getting to block_radius.
            try:
                if get_metro()['multiple_cities']:
                    self.city_slug = args.pop(0)
                else:
                    self.city_slug = ''
                self.street_slug = args.pop(0)
                self.block_range = args.pop(0)
            except IndexError:
                raise FilterError("not enough args")

        try:
            self.block_radius = radius_from_urlfragment(args.pop(0))
        except (TypeError, ValueError):
            raise FilterError('bad radius %r' % self.block_radius)
        except IndexError:
            self.block_radius = context.get('block_radius')
            if self.block_radius is None:
                # Redirect to a URL that includes some radius, either
                # from a cookie, or the default radius.
                # TODO: Filters are used in various contexts, but the
                # redirect URL is tailored only for the schema_filter
                # view.
                xy_radius, block_radius, cookies_to_set = block_radius_value(request)
                raise FilterError('missing radius', url=radius_url(request.path, block_radius))
        if 'block' in kwargs:
            # needs block_radius to already be there.
            self._update_block(kwargs['block'])

        m = re.search('^%s$' % constants.BLOCK_URL_REGEX, self.block_range)
        if not m:
            raise FilterError('Invalid block URL: %r' % self.block_range)
        self.url_to_block_args = m.groups()
        self._got_args = True


    def validate(self):
        # Filtering UI does not provide a page for selecting a block.
        return {}

    def apply(self):
        """filtering by Block.
        """
        if self.location_object is not None:
            block = self.location_object
        else:
            block = url_to_block(self.city_slug, self.street_slug,
                                 *self.url_to_block_args)
            self._update_block(block)
        search_buf = make_search_buffer(block.location.centroid, self.block_radius)
        self.qs = self.qs.filter(location__bboverlaps=search_buf)
        return self.qs


class DateFilter(NewsitemFilter):

    name = 'date'
    date_field_name = 'item_date'
    argname = 'by-date'  # XXX this doesn't feel like it belongs here.

    _sort_value = 1.0

    def __init__(self, request, context, queryset, *args, **kwargs):
        NewsitemFilter.__init__(self, request, context, queryset, *args, **kwargs)
        args = list(args)
        schema = kwargs.get('schema', None) or context.get('schema')
        if schema is not None:
            self.label = schema.date_name
        else:
            self.label = self.name
        gte_kwarg = '%s__gte' % self.date_field_name
        lt_kwarg = '%s__lt' % self.date_field_name
        try:
            start_date, end_date = args
            if isinstance(start_date, basestring):
                start_date = datetime.date(*map(int, start_date.split('-')))
            self.start_date = start_date
            if isinstance(end_date, basestring):
                end_date = datetime.date(*map(int, end_date.split('-')))
            self.end_date = end_date
        except (IndexError, ValueError, TypeError):
            raise FilterError("Missing or invalid date range")

        self.kwargs = {
            gte_kwarg: self.start_date,
            lt_kwarg: self.end_date+datetime.timedelta(days=1)
            }

        if self.start_date == self.end_date:
            self.value = dateformat.format(self.start_date, 'N j, Y')
        else:
            self.value = u'%s - %s' % (dateformat.format(self.start_date, 'N j, Y'), dateformat.format(self.end_date, 'N j, Y'))

        self.short_value = self.value
        self.url = '%s=%s,%s' % (self.argname,
                                 self.start_date.strftime('%Y-%m-%d'),
                                 self.end_date.strftime('%Y-%m-%d'))



    def validate(self):
        # Filtering UI does not provide a page for selecting a block.
        return {}

    def apply(self):
        """ filtering by Date """
        self.qs = self.qs.filter(**self.kwargs)


class PubDateFilter(DateFilter):

    argname = 'by-pub-date'
    date_field_name = 'pub_date'

    _sort_value = 1.0

    def __init__(self, request, context, queryset, *args, **kwargs):
        DateFilter.__init__(self, request, context, queryset, *args, **kwargs)
        self.label = 'date published'


class DuplicateFilterError(FilterError):
    pass

class FilterChain(SortedDict):

    base_url = ''
    schema = None

    def __repr__(self):
        return u'FilterChain(%s)' % SortedDict.__repr__(self)

    def __init__(self, data=None, request=None, context=None, queryset=None, schema=None):
        SortedDict.__init__(self, data=None)
        self.request = request
        self.context = context if context is not None else {}
        self.qs = queryset
        if data is not None:
            # We do this to force our __setitem__ to get called
            # so it will raise error on dupes.
            self.update(data)
        self.lookup_descriptions = []
        self.schema = schema
        if schema:
            self.add('schema', SchemaFilter(request, context, queryset, schema=schema))

    def __setitem__(self, key, value):
        """
        stores a NewsitemFilter, and raises DuplicateFilterError if the
        key exists.
        """
        if self.has_key(key):
            raise DuplicateFilterError(key)
        SortedDict.__setitem__(self, key, value)

    def update(self, dict_):
        # Need this until http://code.djangoproject.com/ticket/15812
        # gets accepted & released.
        if getattr(dict_, 'iteritems', None) is not None:
            dict_ = dict_.iteritems()
        for k, v in dict_:
            # This works for tuples, lists, and other iterators too.
            self[k] = v

    @classmethod
    def from_request(klass, request, context, argstring, filter_sf_dict):
        """Alternate constructor that populates the list of filters
        based on parameters.

        argstring is a string describing the filters (or None, in the case of
        "/filter/").
        """
        # TODO: can we remove some args now that we're not using
        # get_place_info_for_request?
        argstring = urllib.unquote((argstring or '').rstrip('/'))
        argstring = argstring.replace('+', ' ')
        args = []
        chain = klass(schema=context['schema'])

        if argstring and argstring != 'filter':
            for arg in argstring.split(';'):
                try:
                    argname, argvalues = arg.split('=', 1)
                except ValueError:
                    raise FilterError('Invalid filter parameter %r, no equals sign' % arg)
                argname = argname.strip()
                argvalues = [v.strip() for v in argvalues.split(',')]
                if argname:
                    args.append((argname, argvalues))
        else:
            # No filters specified. Do nothing?
            pass

        qs = context['newsitem_qs']
        while args:
            argname, argvalues = args.pop(0)
            argvalues = [v for v in argvalues if v]
            # Date range
            if argname == 'by-date':
                chain['date'] = DateFilter(request, context, qs, *argvalues, schema=chain.schema)
            elif argname == 'by-pub-date':
                chain['date'] = PubDateFilter(request, context, qs, *argvalues, schema=chain.schema)

            # Attribute filtering
            elif argname.startswith('by-'):
                sf_slug = argname[3:]
                try:
                    sf = filter_sf_dict.pop(sf_slug)
                except KeyError:
                    # XXX this will be a confusing error if we already popped it.
                    raise FilterError('Invalid SchemaField slug')
                # Lookup filtering
                if sf.is_lookup:
                    lookup_filter = LookupFilter(request, context, qs, *argvalues,
                                                 schemafield=sf)
                    chain[sf.name] = lookup_filter
                    if lookup_filter.look is not None:
                        chain.lookup_descriptions.append(lookup_filter.look)

                # Boolean attr filtering.
                elif sf.is_type('bool'):
                    chain[sf.name] = BoolFilter(request, context, qs,
                                                *argvalues, schemafield=sf)

                # Text-search attribute filter.
                else:
                    chain[sf.name] = TextSearchFilter(request, context, qs, *argvalues, schemafield=sf)

            # END OF ATTRIBUTE FILTERING

            # Street/address
            elif argname.startswith('streets'):
                chain['location'] = BlockFilter(request, context, qs, *argvalues)
            # Location filtering
            elif argname.startswith('locations'):
                chain['location'] = LocationFilter(request, context, qs, *argvalues)

            else:
                raise FilterError('Invalid filter type')

        return chain

    def validate(self):
        """Check whether any of the filters were requested without
        a required value.  If so, return info about what's needed,
        as a dict.  Stops on the first one that returns anything.

        Can raise FilterError.
        """
        for key, filt in self.items():
            more_needed = filt.validate()
            if more_needed:
                return more_needed
        return {}

    def apply(self, queryset=None):
        """
        Applies each filter in the chain.
        """
        for key, filt in self.normalized_clone().items():
            # TODO: this seems odd. filt.apply() should return the qs?
            if queryset is not None:
                filt.qs = queryset
            filt.apply()
            queryset = filt.qs
        return queryset

    def copy(self):
        # Overriding because default dict.copy() re-inits attributes,
        # and we want copies to be independently mutable.
        clone = self.__class__()
        clone.lookup_descriptions = self.lookup_descriptions[:]
        clone.base_url = self.base_url
        clone.schema = self.schema
        clone.request = self.request
        clone.context = self.context
        clone.update(self)
        return clone

    def normalized_clone(self):
        """
        Return a copy of self with keys in optimal order.
        """
        clone = self.copy()
        clone.clear()
        clone.update(self._sorted_items())
        return clone

    def _sorted_items(self):
        items = self.items()
        return sorted(items, key=lambda item: item[1]._sort_value)

    def replace(self, key, *values):
        """Same as self.add(), but instead of raising DuplicateFilterError
        on existing keys, replaces them.
        """
        if key in self:
            del self[key]
        return self.add(key, *values, _replace=True)

    def add(self, key, *values, **kwargs):
        """Given a string or SchemaField key,
        construct an appropriate NewsitemFilter with the values as arguments,
        and save it as self[key].

        Also for convenience, returns self.

        """

        # TODO: this seems awfully redundant with .from_request().

        # TODO: is this too complex, accepting strings, objects, and
        # arbitrary *values?
        # The isinstance(key, models.SchemaField) clause seems like
        # a good target for moving to a separate API?

        # Unfortunately there's no way to provide a single default arg
        # at the same time as accepting arbitrary *values.
        _replace = kwargs.pop('_replace', False)
        if kwargs:
            raise TypeError("unexpected keyword args %s" % kwargs.keys())

        values = list(values)
        if isinstance(key, models.SchemaField):
            if not values or values == ['']:
                # URL for the page that allows selecting them.
                val = AttributeFilter(self.request, self.context, self.qs, schemafield=key)
                key = key.slug
                if _replace and key in self:
                    del self[key]
                self[key] = val
                return self
            if key.is_lookup:
                values = [LookupFilter(self.request, self.context, self.qs, schemafield=key, *values)]
            elif key.is_type('bool'):
                values = [BoolFilter(self.request, self.context, self.qs, schemafield=key, *values)]
            elif key.is_searchable:
                values = [TextSearchFilter(self.request, self.context, self.qs, schemafield=key, *values)]
            else:
                # Ints, varchars, dates, times, and datetimes.
                values = [AttributeFilter(self.request, self.context, self.qs, schemafield=key, *values)]
            key = key.slug
        if not values:
            raise FilterError("no values passed for arg %s" % key)
        if isinstance(values[0], models.Location):
            val = LocationFilter(self.request, self.context, self.qs, location=values[0])
            key = val.name
        elif isinstance(values[0], ebpub.streets.models.Block):
            block = values.pop(0)
            val = BlockFilter(self.request, self.context, self.qs, *values, block=block)
            key = val.name
        elif isinstance(values[0], models.LocationType):
            val = LocationFilter(self.request, self.context, self.qs, *values[1:], location_type=values[0])
            key = val.name
        elif isinstance(values[0], models.Lookup):
            val = LookupFilter(self.request, self.context, self.qs, values[0],
                               schemafield=values[0].schema_field)
        elif isinstance(values[0], datetime.date):
            if len(values) == 1:
                # start and end are the same date.
                values.append(values[0])
            if values[1] == 'month':
                # TODO: document this!!
                start, end = calendar.monthrange(values[0].year, values[0].month)
                values[0] = values[0].replace(day=start)
                values[1] = values[0].replace(day=end)
            if key == 'pubdate':
                key = 'date'
                val = PubDateFilter(self.request, self.context, self.qs, *values, schema=self.schema)
            else:
                val = DateFilter(self.request, self.context, self.qs, *values, schema=self.schema)
        else:
            # TODO: when does this ever happen?
            val = values[0]
        if _replace and key in self:
            del self[key]
        self[key] = val
        return self

    def make_breadcrumbs(self, additions=(), removals=(), stop_at=None, 
                         base_url=None):
        """
        Returns a list of (label, URL) pairs suitable for making
        breadcrumbs.

        If ``base_url`` is passed, URLs generated will be include that
        that; otherwise fall back to self.base_url; otherwise they
        will just be relative URLs.

        If ``stop_at`` is passed, the key specified will be the last
        one used for the breadcrumb list.

        If ``removals`` is passed, the specified filter keys will be
        excluded from the breadcrumb list.

        If ``additions`` is passed, the specified (key, NewsitemFilter)
        pairs will be added to the end of the breadcrumb list.

        (In some cases, you can pass (key, [args]) and it will figure
        out what kind of NewsitemFilter to create.  TODO: document
        this!!)

        """
        if base_url is None:
            base_url = self.base_url or ''
        # TODO: Can filter_reverse leverage this? Or vice-versa?
        filter_params = []
        clone = self.copy()
        for key in removals:
            try:
                del clone[key]
            except KeyError:
                logger.warn("can't delete nonexistent key %s" % key)

        for key, values in additions:
            clone.replace(key, *values)
        crumbs = []
        for key, filt in clone.items():
            label = getattr(filt, 'short_value', '') or getattr(filt, 'value', '') or getattr(filt, 'label', '')
            if label is None:
                continue
            label = label.title()
            if label and getattr(filt, 'url', None) is not None:
                filter_params.append(filt.url)
                crumbs.append((label, base_url + ';'.join(filter_params)  + '/'))
            if key == stop_at:
                break
        return crumbs

    def make_urls(self, additions=(), removals=(), stop_at=None, base_url=None):
        """
        Just like ``make_breadcrumbs`` but only URLs are included in
        the output.
        """
        crumbs = self.make_breadcrumbs(additions, removals, stop_at, base_url)
        return [crumb[1] for crumb in crumbs]

    def make_url(self, additions=(), removals=(), stop_at=None, base_url=None):
        """
        Makes one URL representing all the filters of this filter chain.
        """
        crumbs = self.make_breadcrumbs(additions, removals, stop_at, base_url)
        if crumbs:
            return crumbs[-1][1]
        else:
            return base_url

    def add_by_place_id(self, pid):
        """
        ``pid`` is a place id string as used by parse_pid and make_pid,
        identifying a location or block (and if a block, a radius).
        """
        place, block_radius, xy_radius = parse_pid(pid)
        if isinstance(place, models.Block):
            self['location'] = BlockFilter(self.request, self.context, self.qs,
                                           block_radius, block=place)
        else:
            self['location'] = LocationFilter(self.request, self.context, self.qs,
                                              location=place)