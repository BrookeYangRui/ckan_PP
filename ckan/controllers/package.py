import logging
from urllib import urlencode
import datetime
import os
import mimetypes
import cgi

from pylons import config
from genshi.template import MarkupTemplate
from genshi.template.text import NewTextTemplate
from paste.deploy.converters import asbool
import paste.fileapp

import ckan.logic as logic
import ckan.lib.base as base
import ckan.lib.maintain as maintain
import ckan.lib.package_saver as package_saver
import ckan.lib.i18n as i18n
import ckan.lib.navl.dictization_functions as dict_fns
import ckan.lib.accept as accept
import ckan.lib.helpers as h
import ckan.model as model
import ckan.lib.datapreview as datapreview
import ckan.lib.plugins
import ckan.lib.uploader as uploader
import ckan.plugins as p
import ckan.lib.render

from ckan.common import OrderedDict, _, json, request, c, g, response
from home import CACHE_PARAMETERS

log = logging.getLogger(__name__)

render = base.render
abort = base.abort
redirect = base.redirect

NotFound = logic.NotFound
NotAuthorized = logic.NotAuthorized
ValidationError = logic.ValidationError
check_access = logic.check_access
get_action = logic.get_action
tuplize_dict = logic.tuplize_dict
clean_dict = logic.clean_dict
parse_params = logic.parse_params
flatten_to_string_key = logic.flatten_to_string_key

lookup_package_plugin = ckan.lib.plugins.lookup_package_plugin


def _encode_params(params):
    return [(k, v.encode('utf-8') if isinstance(v, basestring) else str(v))
            for k, v in params]


def url_with_params(url, params):
    params = _encode_params(params)
    return url + u'?' + urlencode(params)


def search_url(params, package_type=None):
    if not package_type or package_type == 'dataset':
        url = h.url_for(controller='package', action='search')
    else:
        url = h.url_for('{0}_search'.format(package_type))
    return url_with_params(url, params)


class PackageController(base.BaseController):

    def _package_form(self, package_type=None):
        return lookup_package_plugin(package_type).package_form()

    def _setup_template_variables(self, context, data_dict, package_type=None):
        return lookup_package_plugin(package_type).\
            setup_template_variables(context, data_dict)

    def _new_template(self, package_type):
        return lookup_package_plugin(package_type).new_template()

    def _edit_template(self, package_type):
        return lookup_package_plugin(package_type).edit_template()

    def _search_template(self, package_type):
        return lookup_package_plugin(package_type).search_template()

    def _read_template(self, package_type):
        return lookup_package_plugin(package_type).read_template()

    def _history_template(self, package_type):
        return lookup_package_plugin(package_type).history_template()

    def _guess_package_type(self, expecting_name=False):
        """
            Guess the type of package from the URL handling the case
            where there is a prefix on the URL (such as /data/package)
        """

        # Special case: if the rot URL '/' has been redirected to the package
        # controller (e.g. by an IRoutes extension) then there's nothing to do
        # here.
        if request.path == '/':
            return 'dataset'

        parts = [x for x in request.path.split('/') if x]

        idx = -1
        if expecting_name:
            idx = -2

        pt = parts[idx]
        if pt == 'package':
            pt = 'dataset'

        return pt

    def search(self):
        from ckan.lib.search import SearchError

        package_type = self._guess_package_type()

        try:
            context = {'model': model, 'user': c.user or c.author,
                       'auth_user_obj': c.userobj}
            check_access('site_read', context)
        except NotAuthorized:
            abort(401, _('Not authorized to see this page'))

        # unicode format (decoded from utf8)
        q = c.q = request.params.get('q', u'')
        c.query_error = False
        try:
            page = int(request.params.get('page', 1))
        except ValueError, e:
            abort(400, ('"page" parameter must be an integer'))
        limit = g.datasets_per_page

        # most search operations should reset the page counter:
        params_nopage = [(k, v) for k, v in request.params.items()
                         if k != 'page']

        def drill_down_url(alternative_url=None, **by):
            return h.add_url_param(alternative_url=alternative_url,
                                   controller='package', action='search',
                                   new_params=by)

        c.drill_down_url = drill_down_url

        def remove_field(key, value=None, replace=None):
            return h.remove_url_param(key, value=value, replace=replace,
                                  controller='package', action='search')

        c.remove_field = remove_field

        sort_by = request.params.get('sort', None)
        params_nosort = [(k, v) for k, v in params_nopage if k != 'sort']

        def _sort_by(fields):
            """
            Sort by the given list of fields.

            Each entry in the list is a 2-tuple: (fieldname, sort_order)

            eg - [('metadata_modified', 'desc'), ('name', 'asc')]

            If fields is empty, then the default ordering is used.
            """
            params = params_nosort[:]

            if fields:
                sort_string = ', '.join('%s %s' % f for f in fields)
                params.append(('sort', sort_string))
            return search_url(params, package_type)

        c.sort_by = _sort_by
        if sort_by is None:
            c.sort_by_fields = []
        else:
            c.sort_by_fields = [field.split()[0]
                                for field in sort_by.split(',')]

        def pager_url(q=None, page=None):
            params = list(params_nopage)
            params.append(('page', page))
            return search_url(params, package_type)

        c.search_url_params = urlencode(_encode_params(params_nopage))

        try:
            c.fields = []
            # c.fields_grouped will contain a dict of params containing
            # a list of values eg {'tags':['tag1', 'tag2']}
            c.fields_grouped = {}
            search_extras = {}
            fq = ''
            for (param, value) in request.params.items():
                if param not in ['q', 'page', 'sort'] \
                        and len(value) and not param.startswith('_'):
                    if not param.startswith('ext_'):
                        c.fields.append((param, value))
                        fq += ' %s:"%s"' % (param, value)
                        if param not in c.fields_grouped:
                            c.fields_grouped[param] = [value]
                        else:
                            c.fields_grouped[param].append(value)
                    else:
                        search_extras[param] = value

            context = {'model': model, 'session': model.Session,
                       'user': c.user or c.author, 'for_view': True,
                       'auth_user_obj': c.userobj}

            if package_type and package_type != 'dataset':
                # Only show datasets of this particular type
                fq += ' +dataset_type:{type}'.format(type=package_type)
            else:
                # Unless changed via config options, don't show non standard
                # dataset types on the default search page
                if not asbool(config.get('ckan.search.show_all_types', 'False')):
                    fq += ' +dataset_type:dataset'

            facets = OrderedDict()

            default_facet_titles = {
                    'organization': _('Organizations'),
                    'groups': _('Groups'),
                    'tags': _('Tags'),
                    'res_format': _('Formats'),
                    'license_id': _('Licenses'),
                    }

            for facet in g.facets:
                if facet in default_facet_titles:
                    facets[facet] = default_facet_titles[facet]
                else:
                    facets[facet] = facet

            # Facet titles
            for plugin in p.PluginImplementations(p.IFacets):
                facets = plugin.dataset_facets(facets, package_type)

            c.facet_titles = facets

            data_dict = {
                'q': q,
                'fq': fq.strip(),
                'facet.field': facets.keys(),
                'rows': limit,
                'start': (page - 1) * limit,
                'sort': sort_by,
                'extras': search_extras
            }

            query = get_action('package_search')(context, data_dict)
            c.sort_by_selected = query['sort']

            c.page = h.Page(
                collection=query['results'],
                page=page,
                url=pager_url,
                item_count=query['count'],
                items_per_page=limit
            )
            c.facets = query['facets']
            c.search_facets = query['search_facets']
            c.page.items = query['results']
        except SearchError, se:
            log.error('Dataset search error: %r', se.args)
            c.query_error = True
            c.facets = {}
            c.search_facets = {}
            c.page = h.Page(collection=[])
        c.search_facets_limits = {}
        for facet in c.search_facets.keys():
            try:
                limit = int(request.params.get('_%s_limit' % facet,
                                               g.facets_default_number))
            except ValueError:
                abort(400, _('Parameter "{parameter_name}" is not '
                             'an integer').format(
                                 parameter_name='_%s_limit' % facet
                             ))
            c.search_facets_limits[facet] = limit

        maintain.deprecate_context_item(
          'facets',
          'Use `c.search_facets` instead.')

        self._setup_template_variables(context, {},
                                       package_type=package_type)

        return render(self._search_template(package_type))

    def _content_type_from_extension(self, ext):
        ct, mu, ext = accept.parse_extension(ext)
        if not ct:
            return None, None, None,
        return ct, ext, (NewTextTemplate, MarkupTemplate)[mu]

    def _content_type_from_accept(self):
        """
        Given a requested format this method determines the content-type
        to set and the genshi template loader to use in order to render
        it accurately.  TextTemplate must be used for non-xml templates
        whilst all that are some sort of XML should use MarkupTemplate.
        """
        ct, mu, ext = accept.parse_header(request.headers.get('Accept', ''))
        return ct, ext, (NewTextTemplate, MarkupTemplate)[mu]

    def resources(self, id):
        package_type = self._get_package_type(id.split('@')[0])
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj}
        data_dict = {'id': id}

        try:
            check_access('package_update', context, data_dict)
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized, e:
            abort(401, _('User %r not authorized to edit %s') % (c.user, id))
        # check if package exists
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
            c.pkg = context['package']
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % id)

        self._setup_template_variables(context, {'id': id},
                                       package_type=package_type)

        return render('package/resources.html')

    def read(self, id, format='html'):
        if not format == 'html':
            ctype, extension, loader = \
                self._content_type_from_extension(format)
            if not ctype:
                # An unknown format, we'll carry on in case it is a
                # revision specifier and re-constitute the original id
                id = "%s.%s" % (id, format)
                ctype, format, loader = "text/html; charset=utf-8", "html", \
                    MarkupTemplate
        else:
            ctype, format, loader = self._content_type_from_accept()

        response.headers['Content-Type'] = ctype

        package_type = self._get_package_type(id.split('@')[0])
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj}
        data_dict = {'id': id}

        # interpret @<revision_id> or @<date> suffix
        split = id.split('@')
        if len(split) == 2:
            data_dict['id'], revision_ref = split
            if model.is_id(revision_ref):
                context['revision_id'] = revision_ref
            else:
                try:
                    date = h.date_str_to_datetime(revision_ref)
                    context['revision_date'] = date
                except TypeError, e:
                    abort(400, _('Invalid revision format: %r') % e.args)
                except ValueError, e:
                    abort(400, _('Invalid revision format: %r') % e.args)
        elif len(split) > 2:
            abort(400, _('Invalid revision format: %r') %
                  'Too many "@" symbols')

        # check if package exists
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
            c.pkg = context['package']
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % id)

        # used by disqus plugin
        c.current_package_id = c.pkg.id
        c.related_count = c.pkg.related_count

        # can the resources be previewed?
        for resource in c.pkg_dict['resources']:
            # Backwards compatibility with preview interface
            resource['can_be_previewed'] = self._resource_preview(
                {'resource': resource, 'package': c.pkg_dict})

            resource_views = get_action('resource_view_list')(
                context, {'id': resource['id']})
            resource['has_views'] = len(resource_views) > 0

        self._setup_template_variables(context, {'id': id},
                                       package_type=package_type)

        package_saver.PackageSaver().render_package(c.pkg_dict, context)

        template = self._read_template(package_type)
        template = template[:template.index('.') + 1] + format

        try:
            return render(template, loader_class=loader)
        except ckan.lib.render.TemplateNotFound:
            msg = _("Viewing {package_type} datasets in {format} format is "
                    "not supported (template file {file} not found).".format(
                    package_type=package_type, format=format, file=template))
            abort(404, msg)

        assert False, "We should never get here"


    def history(self, id):
        package_type = self._get_package_type(id.split('@')[0])

        if 'diff' in request.params or 'selected1' in request.params:
            try:
                params = {'id': request.params.getone('pkg_name'),
                          'diff': request.params.getone('selected1'),
                          'oldid': request.params.getone('selected2'),
                          }
            except KeyError, e:
                if 'pkg_name' in dict(request.params):
                    id = request.params.getone('pkg_name')
                c.error = \
                    _('Select two revisions before doing the comparison.')
            else:
                params['diff_entity'] = 'package'
                h.redirect_to(controller='revision', action='diff', **params)

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}
        data_dict = {'id': id}
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
            c.pkg_revisions = get_action('package_revision_list')(context,
                                                                  data_dict)
            # TODO: remove
            # Still necessary for the authz check in group/layout.html
            c.pkg = context['package']

        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound:
            abort(404, _('Dataset not found'))

        format = request.params.get('format', '')
        if format == 'atom':
            # Generate and return Atom 1.0 document.
            from webhelpers.feedgenerator import Atom1Feed
            feed = Atom1Feed(
                title=_(u'CKAN Dataset Revision History'),
                link=h.url_for(controller='revision', action='read',
                               id=c.pkg_dict['name']),
                description=_(u'Recent changes to CKAN Dataset: ') +
                (c.pkg_dict['title'] or ''),
                language=unicode(i18n.get_lang()),
            )
            for revision_dict in c.pkg_revisions:
                revision_date = h.date_str_to_datetime(
                    revision_dict['timestamp'])
                try:
                    dayHorizon = int(request.params.get('days'))
                except:
                    dayHorizon = 30
                dayAge = (datetime.datetime.now() - revision_date).days
                if dayAge >= dayHorizon:
                    break
                if revision_dict['message']:
                    item_title = u'%s' % revision_dict['message'].\
                        split('\n')[0]
                else:
                    item_title = u'%s' % revision_dict['id']
                item_link = h.url_for(controller='revision', action='read',
                                      id=revision_dict['id'])
                item_description = _('Log message: ')
                item_description += '%s' % (revision_dict['message'] or '')
                item_author_name = revision_dict['author']
                item_pubdate = revision_date
                feed.add_item(
                    title=item_title,
                    link=item_link,
                    description=item_description,
                    author_name=item_author_name,
                    pubdate=item_pubdate,
                )
            response.headers['Content-Type'] = 'application/atom+xml'
            return feed.writeString('utf-8')

        c.related_count = c.pkg.related_count
        return render(self._history_template(c.pkg_dict.get('type',
                                                            package_type)))

    def new(self, data=None, errors=None, error_summary=None):
        package_type = self._guess_package_type(True)

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj,
                   'save': 'save' in request.params}

        # Package needs to have a organization group in the call to
        # check_access and also to save it
        try:
            check_access('package_create', context)
        except NotAuthorized:
            abort(401, _('Unauthorized to create a package'))

        if context['save'] and not data:
            return self._save_new(context, package_type=package_type)

        data = data or clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(
            request.params, ignore_keys=CACHE_PARAMETERS))))
        c.resources_json = h.json.dumps(data.get('resources', []))
        # convert tags if not supplied in data
        if data and not data.get('tag_string'):
            data['tag_string'] = ', '.join(
                h.dict_list_reduce(data.get('tags', {}), 'name'))

        errors = errors or {}
        error_summary = error_summary or {}
        # in the phased add dataset we need to know that
        # we have already completed stage 1
        stage = ['active']
        if data.get('state', '').startswith('draft'):
            stage = ['active', 'complete']

        # if we are creating from a group then this allows the group to be
        # set automatically
        data['group_id'] = request.params.get('group') or \
            request.params.get('groups__0__id')

        vars = {'data': data, 'errors': errors,
                'error_summary': error_summary,
                'action': 'new', 'stage': stage}
        c.errors_json = h.json.dumps(errors)

        self._setup_template_variables(context, {},
                                       package_type=package_type)

        # TODO: This check is to maintain backwards compatibility with the
        # old way of creating custom forms. This behaviour is now deprecated.
        if hasattr(self, 'package_form'):
            c.form = render(self.package_form, extra_vars=vars)
        else:
            c.form = render(self._package_form(package_type=package_type),
                            extra_vars=vars)
        return render(self._new_template(package_type),
                      extra_vars={'stage': stage})

    def resource_edit(self, id, resource_id, data=None, errors=None,
                      error_summary=None):
        if request.method == 'POST' and not data:
            data = data or clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(
                request.POST))))
            # we don't want to include save as it is part of the form
            del data['save']

            context = {'model': model, 'session': model.Session,
                       'api_version': 3, 'for_edit': True,
                       'user': c.user or c.author, 'auth_user_obj': c.userobj}

            data['package_id'] = id
            try:
                if resource_id:
                    data['id'] = resource_id
                    get_action('resource_update')(context, data)
                else:
                    get_action('resource_create')(context, data)
            except ValidationError, e:
                errors = e.error_dict
                error_summary = e.error_summary
                return self.resource_edit(id, resource_id, data,
                                          errors, error_summary)
            except NotAuthorized:
                abort(401, _('Unauthorized to edit this resource'))
            redirect(h.url_for(controller='package', action='resource_read',
                               id=id, resource_id=resource_id))

        context = {'model': model, 'session': model.Session,
                   'api_version': 3, 'for_edit': True,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}
        pkg_dict = get_action('package_show')(context, {'id': id})
        if pkg_dict['state'].startswith('draft'):
            # dataset has not yet been fully created
            resource_dict = get_action('resource_show')(context, {'id': resource_id})
            fields = ['url', 'resource_type', 'format', 'name', 'description', 'id']
            data = {}
            for field in fields:
                data[field] = resource_dict[field]
            return self.new_resource(id, data=data)
        # resource is fully created
        try:
            resource_dict = get_action('resource_show')(context, {'id': resource_id})
        except NotFound:
            abort(404, _('Resource not found'))
        c.pkg_dict = pkg_dict
        c.resource = resource_dict
        # set the form action
        c.form_action = h.url_for(controller='package',
                                  action='resource_edit',
                                  resource_id=resource_id,
                                  id=id)
        if not data:
            data = resource_dict

        errors = errors or {}
        error_summary = error_summary or {}
        vars = {'data': data, 'errors': errors,
                'error_summary': error_summary, 'action': 'new'}
        return render('package/resource_edit.html', extra_vars=vars)

    def new_resource(self, id, data=None, errors=None, error_summary=None):
        ''' FIXME: This is a temporary action to allow styling of the
        forms. '''
        if request.method == 'POST' and not data:
            save_action = request.params.get('save')
            data = data or clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(
                request.POST))))
            # we don't want to include save as it is part of the form
            del data['save']
            resource_id = data['id']
            del data['id']

            context = {'model': model, 'session': model.Session,
                       'user': c.user or c.author, 'auth_user_obj': c.userobj}

            # see if we have any data that we are trying to save
            data_provided = False
            for key, value in data.iteritems():
                if ((value or isinstance(value, cgi.FieldStorage))
                    and key != 'resource_type'):
                    data_provided = True
                    break

            if not data_provided and save_action != "go-dataset-complete":
                if save_action == 'go-dataset':
                    # go to final stage of adddataset
                    redirect(h.url_for(controller='package',
                                       action='edit', id=id))
                # see if we have added any resources
                try:
                    data_dict = get_action('package_show')(context, {'id': id})
                except NotAuthorized:
                    abort(401, _('Unauthorized to update dataset'))
                except NotFound:
                    abort(404,
                      _('The dataset {id} could not be found.').format(id=id))
                if not len(data_dict['resources']):
                    # no data so keep on page
                    msg = _('You must add at least one data resource')
                    # On new templates do not use flash message
                    if g.legacy_templates:
                        h.flash_error(msg)
                        redirect(h.url_for(controller='package',
                                           action='new_resource', id=id))
                    else:
                        errors = {}
                        error_summary = {_('Error'): msg}
                        return self.new_resource(id, data, errors, error_summary)
                # we have a resource so let them add metadata
                redirect(h.url_for(controller='package',
                                   action='new_metadata', id=id))

            data['package_id'] = id
            try:
                if resource_id:
                    data['id'] = resource_id
                    get_action('resource_update')(context, data)
                else:
                    get_action('resource_create')(context, data)
            except ValidationError, e:
                errors = e.error_dict
                error_summary = e.error_summary
                return self.new_resource(id, data, errors, error_summary)
            except NotAuthorized:
                abort(401, _('Unauthorized to create a resource'))
            except NotFound:
                abort(404,
                    _('The dataset {id} could not be found.').format(id=id))
            if save_action == 'go-metadata':
                # XXX race condition if another user edits/deletes
                data_dict = get_action('package_show')(context, {'id': id})
                get_action('package_update')(
                    dict(context, allow_state_change=True),
                    dict(data_dict, state='active'))
                redirect(h.url_for(controller='package',
                                   action='read', id=id))
            elif save_action == 'go-dataset':
                # go to first stage of add dataset
                redirect(h.url_for(controller='package',
                                   action='edit', id=id))
            elif save_action == 'go-dataset-complete':
                # go to first stage of add dataset
                redirect(h.url_for(controller='package',
                                   action='read', id=id))
            else:
                # add more resources
                redirect(h.url_for(controller='package',
                                   action='new_resource', id=id))
        errors = errors or {}
        error_summary = error_summary or {}
        vars = {'data': data, 'errors': errors,
                'error_summary': error_summary, 'action': 'new'}
        vars['pkg_name'] = id
        # get resources for sidebar
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}
        try:
            pkg_dict = get_action('package_show')(context, {'id': id})
        except NotFound:
            abort(404, _('The dataset {id} could not be found.').format(id=id))
        try:
            check_access('resource_create', context, pkg_dict)
        except NotAuthorized:
            abort(401, _('Unauthorized to create a resource for this package'))

        # required for nav menu
        vars['pkg_dict'] = pkg_dict
        template = 'package/new_resource_not_draft.html'
        if pkg_dict['state'].startswith('draft'):
            vars['stage'] = ['complete', 'active']
            template = 'package/new_resource.html'
        return render(template, extra_vars=vars)

    def edit(self, id, data=None, errors=None, error_summary=None):
        package_type = self._get_package_type(id)
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj,
                   'save': 'save' in request.params,
                   'moderated': config.get('moderated'),
                   'pending': True}

        if context['save'] and not data:
            return self._save_edit(id, context, package_type=package_type)
        try:
            c.pkg_dict = get_action('package_show')(context, {'id': id})
            context['for_edit'] = True
            old_data = get_action('package_show')(context, {'id': id})
            # old data is from the database and data is passed from the
            # user if there is a validation error. Use users data if there.
            if data:
                old_data.update(data)
            data = old_data
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound:
            abort(404, _('Dataset not found'))
        # are we doing a multiphase add?
        if data.get('state', '').startswith('draft'):
            c.form_action = h.url_for(controller='package', action='new')
            c.form_style = 'new'
            return self.new(data=data, errors=errors,
                            error_summary=error_summary)

        c.pkg = context.get("package")
        c.resources_json = h.json.dumps(data.get('resources', []))

        try:
            check_access('package_update', context)
        except NotAuthorized, e:
            abort(401, _('User %r not authorized to edit %s') % (c.user, id))
        # convert tags if not supplied in data
        if data and not data.get('tag_string'):
            data['tag_string'] = ', '.join(h.dict_list_reduce(
                c.pkg_dict.get('tags', {}), 'name'))
        errors = errors or {}
        vars = {'data': data, 'errors': errors,
                'error_summary': error_summary, 'action': 'edit'}
        c.errors_json = h.json.dumps(errors)

        self._setup_template_variables(context, {'id': id},
                                       package_type=package_type)
        c.related_count = c.pkg.related_count

        # we have already completed stage 1
        vars['stage'] = ['active']
        if data.get('state', '').startswith('draft'):
            vars['stage'] = ['active', 'complete']

        # TODO: This check is to maintain backwards compatibility with the
        # old way of creating custom forms. This behaviour is now deprecated.
        if hasattr(self, 'package_form'):
            c.form = render(self.package_form, extra_vars=vars)
        else:
            c.form = render(self._package_form(package_type=package_type),
                            extra_vars=vars)

        return render(self._edit_template(package_type),
                      extra_vars={'stage': vars['stage']})

    def read_ajax(self, id, revision=None):
        package_type = self._get_package_type(id)
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj,
                   'revision_id': revision}
        try:
            data = get_action('package_show')(context, {'id': id})
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound:
            abort(404, _('Dataset not found'))

        data.pop('tags')
        data = flatten_to_string_key(data)
        response.headers['Content-Type'] = 'application/json;charset=utf-8'
        return h.json.dumps(data)

    def history_ajax(self, id):

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}
        data_dict = {'id': id}
        try:
            pkg_revisions = get_action('package_revision_list')(
                context, data_dict)
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound:
            abort(404, _('Dataset not found'))

        data = []
        approved = False
        for num, revision in enumerate(pkg_revisions):
            if not approved and revision['approved_timestamp']:
                current_approved, approved = True, True
            else:
                current_approved = False

            data.append({'revision_id': revision['id'],
                         'message': revision['message'],
                         'timestamp': revision['timestamp'],
                         'author': revision['author'],
                         'approved': bool(revision['approved_timestamp']),
                         'current_approved': current_approved})

        response.headers['Content-Type'] = 'application/json;charset=utf-8'
        return h.json.dumps(data)

    def _get_package_type(self, id):
        """
        Given the id of a package it determines the plugin to load
        based on the package's type name (type). The plugin found
        will be returned, or None if there is no plugin associated with
        the type.
        """
        pkg = model.Package.get(id)
        if pkg:
            return pkg.type or 'dataset'
        return None

    def _tag_string_to_list(self, tag_string):
        ''' This is used to change tags from a sting to a list of dicts '''
        out = []
        for tag in tag_string.split(','):
            tag = tag.strip()
            if tag:
                out.append({'name': tag,
                            'state': 'active'})
        return out

    def _save_new(self, context, package_type=None):
        # The staged add dataset used the new functionality when the dataset is
        # partially created so we need to know if we actually are updating or
        # this is a real new.
        is_an_update = False
        ckan_phase = request.params.get('_ckan_phase')
        from ckan.lib.search import SearchIndexError
        try:
            data_dict = clean_dict(dict_fns.unflatten(
                tuplize_dict(parse_params(request.POST))))
            if ckan_phase:
                # prevent clearing of groups etc
                context['allow_partial_update'] = True
                # sort the tags
                data_dict['tags'] = self._tag_string_to_list(
                    data_dict['tag_string'])
                if data_dict.get('pkg_name'):
                    is_an_update = True
                    # This is actually an update not a save
                    data_dict['id'] = data_dict['pkg_name']
                    del data_dict['pkg_name']
                    # don't change the dataset state
                    data_dict['state'] = 'draft'
                    # this is actually an edit not a save
                    pkg_dict = get_action('package_update')(context, data_dict)

                    if request.params['save'] == 'go-metadata':
                        # redirect to add metadata
                        url = h.url_for(controller='package',
                                        action='new_metadata',
                                        id=pkg_dict['name'])
                    else:
                        # redirect to add dataset resources
                        url = h.url_for(controller='package',
                                        action='new_resource',
                                        id=pkg_dict['name'])
                    redirect(url)
                # Make sure we don't index this dataset
                if request.params['save'] not in ['go-resource', 'go-metadata']:
                    data_dict['state'] = 'draft'
                # allow the state to be changed
                context['allow_state_change'] = True

            data_dict['type'] = package_type
            context['message'] = data_dict.get('log_message', '')
            pkg_dict = get_action('package_create')(context, data_dict)

            if ckan_phase:
                # redirect to add dataset resources
                url = h.url_for(controller='package',
                                action='new_resource',
                                id=pkg_dict['name'])
                redirect(url)

            self._form_save_redirect(pkg_dict['name'], 'new', package_type=package_type)
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % '')
        except NotFound, e:
            abort(404, _('Dataset not found'))
        except dict_fns.DataError:
            abort(400, _(u'Integrity Error'))
        except SearchIndexError, e:
            try:
                exc_str = unicode(repr(e.args))
            except Exception:  # We don't like bare excepts
                exc_str = unicode(str(e))
            abort(500, _(u'Unable to add package to search index.') + exc_str)
        except ValidationError, e:
            errors = e.error_dict
            error_summary = e.error_summary
            if is_an_update:
                # we need to get the state of the dataset to show the stage we
                # are on.
                pkg_dict = get_action('package_show')(context, data_dict)
                data_dict['state'] = pkg_dict['state']
                return self.edit(data_dict['id'], data_dict,
                                 errors, error_summary)
            data_dict['state'] = 'none'
            return self.new(data_dict, errors, error_summary)

    def _save_edit(self, name_or_id, context, package_type=None):
        from ckan.lib.search import SearchIndexError
        log.debug('Package save request name: %s POST: %r',
                  name_or_id, request.POST)
        try:
            data_dict = clean_dict(dict_fns.unflatten(
                tuplize_dict(parse_params(request.POST))))
            if '_ckan_phase' in data_dict:
                # we allow partial updates to not destroy existing resources
                context['allow_partial_update'] = True
                data_dict['tags'] = self._tag_string_to_list(
                    data_dict['tag_string'])
                del data_dict['_ckan_phase']
                del data_dict['save']
            context['message'] = data_dict.get('log_message', '')
            if not context['moderated']:
                context['pending'] = False
            data_dict['id'] = name_or_id
            pkg = get_action('package_update')(context, data_dict)
            if request.params.get('save', '') == 'Approve':
                get_action('make_latest_pending_package_active')(
                    context, data_dict)
            c.pkg = context['package']
            c.pkg_dict = pkg

            self._form_save_redirect(pkg['name'], 'edit', package_type=package_type)
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % id)
        except NotFound, e:
            abort(404, _('Dataset not found'))
        except dict_fns.DataError:
            abort(400, _(u'Integrity Error'))
        except SearchIndexError, e:
            try:
                exc_str = unicode(repr(e.args))
            except Exception:  # We don't like bare excepts
                exc_str = unicode(str(e))
            abort(500, _(u'Unable to update search index.') + exc_str)
        except ValidationError, e:
            errors = e.error_dict
            error_summary = e.error_summary
            return self.edit(name_or_id, data_dict, errors, error_summary)

    def _form_save_redirect(self, pkgname, action, package_type=None):
        '''This redirects the user to the CKAN package/read page,
        unless there is request parameter giving an alternate location,
        perhaps an external website.
        @param pkgname - Name of the package just edited
        @param action - What the action of the edit was
        '''
        assert action in ('new', 'edit')
        url = request.params.get('return_to') or \
            config.get('package_%s_return_url' % action)
        if url:
            url = url.replace('<NAME>', pkgname)
        else:
            if package_type is None or package_type == 'dataset':
                url = h.url_for(controller='package', action='read', id=pkgname)
            else:
                url = h.url_for('{0}_read'.format(package_type), id=pkgname)
        redirect(url)

    def _adjust_license_id_options(self, pkg, fs):
        options = fs.license_id.render_opts['options']
        is_included = False
        for option in options:
            license_id = option[1]
            if license_id == pkg.license_id:
                is_included = True
        if not is_included:
            options.insert(1, (pkg.license_id, pkg.license_id))

    def delete(self, id):

        if 'cancel' in request.params:
            h.redirect_to(controller='package', action='edit', id=id)

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}

        try:
            check_access('package_delete', context, {'id': id})
        except NotAuthorized:
            abort(401, _('Unauthorized to delete package %s') % '')

        try:
            if request.method == 'POST':
                get_action('package_delete')(context, {'id': id})
                h.flash_notice(_('Dataset has been deleted.'))
                h.redirect_to(controller='package', action='search')
            c.pkg_dict = get_action('package_show')(context, {'id': id})
        except NotAuthorized:
            abort(401, _('Unauthorized to delete package %s') % '')
        except NotFound:
            abort(404, _('Dataset not found'))
        return render('package/confirm_delete.html')

    def resource_delete(self, id, resource_id):

        if 'cancel' in request.params:
            h.redirect_to(controller='package', action='resource_edit', resource_id=resource_id, id=id)

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}

        try:
            check_access('package_delete', context, {'id': id})
        except NotAuthorized:
            abort(401, _('Unauthorized to delete package %s') % '')

        try:
            if request.method == 'POST':
                get_action('resource_delete')(context, {'id': resource_id})
                h.flash_notice(_('Resource has been deleted.'))
                h.redirect_to(controller='package', action='read', id=id)
            c.resource_dict = get_action('resource_show')(context, {'id': resource_id})
            c.pkg_id = id
        except NotAuthorized:
            abort(401, _('Unauthorized to delete resource %s') % '')
        except NotFound:
            abort(404, _('Resource not found'))
        return render('package/confirm_delete_resource.html')

    def autocomplete(self):
        '''Deprecated in favour of /api/2/util/dataset/autocomplete'''
        q = unicode(request.params.get('q', ''))
        if not len(q):
            return ''

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}

        data_dict = {'q': q}
        packages = get_action('package_autocomplete')(context, data_dict)

        pkg_list = []
        for pkg in packages:
            pkg_list.append('%s|%s' % (pkg['match_displayed'].
                                       replace('|', ' '), pkg['name']))
        return '\n'.join(pkg_list)

    def _render_edit_form(self, fs, params={}, clear_session=False):
        # errors arrive in c.error and fs.errors
        c.log_message = params.get('log_message', '')
        # rgrp: expunge everything from session before dealing with
        # validation errors) so we don't have any problematic saves
        # when the fs.render causes a flush.
        # seb: If the session is *expunged*, then the form can't be
        # rendered; I've settled with a rollback for now, which isn't
        # necessarily what's wanted here.
        # dread: I think this only happened with tags because until
        # this changeset, Tag objects were created in the Renderer
        # every time you hit preview. So I don't believe we need to
        # clear the session any more. Just in case I'm leaving it in
        # with the log comments to find out.
        if clear_session:
            # log to see if clearing the session is ever required
            if model.Session.new or model.Session.dirty or \
                    model.Session.deleted:
                log.warn('Expunging session changes which were not expected: '
                         '%r %r %r', (model.Session.new, model.Session.dirty,
                                      model.Session.deleted))
            try:
                model.Session.rollback()
            except AttributeError:
                # older SQLAlchemy versions
                model.Session.clear()
        edit_form_html = fs.render()
        c.form = h.literal(edit_form_html)
        return h.literal(render('package/edit_form.html'))

    def _update_authz(self, fs):
        validation = fs.validate()
        if not validation:
            c.form = self._render_edit_form(fs, request.params)
            raise package_saver.ValidationException(fs)
        try:
            fs.sync()
        except Exception, inst:
            model.Session.rollback()
            raise
        else:
            model.Session.commit()

    def resource_read(self, id, resource_id):
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}

        try:
            c.resource = get_action('resource_show')(context,
                                                     {'id': resource_id})
            c.package = get_action('package_show')(context, {'id': id})
            # required for nav menu
            c.pkg = context['package']
            c.pkg_dict = c.package
        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % id)
        # get package license info
        license_id = c.package.get('license_id')
        try:
            c.package['isopen'] = model.Package.\
                get_license_register()[license_id].isopen()
        except KeyError:
            c.package['isopen'] = False

        # TODO: find a nicer way of doing this
        c.datastore_api = '%s/api/action' % config.get('ckan.site_url', '').rstrip('/')

        c.related_count = c.pkg.related_count

        c.resource['can_be_previewed'] = self._resource_preview(
            {'resource': c.resource, 'package': c.package})

        resource_views = get_action('resource_view_list')(
            context, {'id': resource_id})
        c.resource['has_views'] = len(resource_views) > 0

        current_resource_view = None
        view_id = request.GET.get('view_id')
        if c.resource['can_be_previewed'] and not view_id:
            current_resource_view = None
        elif c.resource['has_views']:
            if view_id:
                current_resource_view = [rv for rv in resource_views
                                         if rv['id'] == view_id]
                if len(current_resource_view) == 1:
                    current_resource_view = current_resource_view[0]
                else:
                    abort(404, _('Resource view not found'))
            else:
                current_resource_view = resource_views[0]

        vars = {'resource_views': resource_views,
                'current_resource_view': current_resource_view}

        return render('package/resource_read.html', extra_vars=vars)

    @maintain.deprecated('Resource preview is deprecated. Please use the new '
                         'resource views')
    def _resource_preview(self, data_dict):
        '''Deprecated in 2.3'''
        return bool(datapreview.get_preview_plugin(data_dict, return_first=True))

    def resource_download(self, id, resource_id, filename=None):
        """
        Provides a direct download by either redirecting the user to the url stored
         or downloading an uploaded file directly.
        """
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}

        try:
            rsc = get_action('resource_show')(context, {'id': resource_id})
            pkg = get_action('package_show')(context, {'id': id})
        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % id)

        if rsc.get('url_type') == 'upload':
            upload = uploader.ResourceUpload(rsc)
            filepath = upload.get_path(rsc['id'])
            fileapp = paste.fileapp.FileApp(filepath)
            try:
               status, headers, app_iter = request.call_application(fileapp)
            except OSError:
               abort(404, _('Resource data not found'))
            response.headers.update(dict(headers))
            content_type, content_enc = mimetypes.guess_type(rsc.get('url',''))
            if content_type:
                response.headers['Content-Type'] = content_type
            response.status = status
            return app_iter
        elif not 'url' in rsc:
            abort(404, _('No download is available'))
        redirect(rsc['url'])

    def follow(self, id):
        '''Start following this dataset.'''
        context = {'model': model,
                   'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}
        data_dict = {'id': id}
        try:
            get_action('follow_dataset')(context, data_dict)
            package_dict = get_action('package_show')(context, data_dict)
            h.flash_success(_("You are now following {0}").format(
                package_dict['title']))
        except ValidationError as e:
            error_message = (e.extra_msg or e.message or e.error_summary
                    or e.error_dict)
            h.flash_error(error_message)
        except NotAuthorized as e:
            h.flash_error(e.extra_msg)
        h.redirect_to(controller='package', action='read', id=id)

    def unfollow(self, id):
        '''Stop following this dataset.'''
        context = {'model': model,
                   'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}
        data_dict = {'id': id}
        try:
            get_action('unfollow_dataset')(context, data_dict)
            package_dict = get_action('package_show')(context, data_dict)
            h.flash_success(_("You are no longer following {0}").format(
                package_dict['title']))
        except ValidationError as e:
            error_message = (e.extra_msg or e.message or e.error_summary
                    or e.error_dict)
            h.flash_error(error_message)
        except (NotFound, NotAuthorized) as e:
            error_message = e.extra_msg or e.message
            h.flash_error(error_message)
        h.redirect_to(controller='package', action='read', id=id)

    def followers(self, id=None):
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj}

        data_dict = {'id': id}
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
            c.pkg = context['package']
            c.followers = get_action('dataset_follower_list')(context,
                    {'id': c.pkg_dict['id']})

            c.related_count = c.pkg.related_count
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read package %s') % id)

        return render('package/followers.html')

    def groups(self, id):
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj, 'use_cache': False}
        data_dict = {'id': id}
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read dataset %s') % id)

        if request.method == 'POST':
            new_group = request.POST.get('group_added')
            if new_group:
                data_dict = {"id": new_group,
                             "object": id,
                             "object_type": 'package',
                             "capacity": 'public'}
                try:
                    get_action('member_create')(context, data_dict)
                except NotFound:
                    abort(404, _('Group not found'))

            removed_group = None
            for param in request.POST:
                if param.startswith('group_remove'):
                    removed_group = param.split('.')[-1]
                    break
            if removed_group:
                data_dict = {"id": removed_group,
                             "object": id,
                             "object_type": 'package'}

                try:
                    get_action('member_delete')(context, data_dict)
                except NotFound:
                    abort(404, _('Group not found'))
            redirect(h.url_for(controller='package',
                               action='groups', id=id))



        context['is_member'] = True
        users_groups = get_action('group_list_authz')(context, data_dict)

        pkg_group_ids = set(group['id'] for group
                         in c.pkg_dict.get('groups', []))
        user_group_ids = set(group['id'] for group
                          in users_groups)

        c.group_dropdown = [[group['id'], group['display_name']]
                           for group in users_groups if
                           group['id'] not in pkg_group_ids]

        for group in c.pkg_dict.get('groups', []):
            group['user_member'] = (group['id'] in user_group_ids)

        return render('package/group_list.html')

    def activity(self, id):
        '''Render this package's public activity stream page.'''

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj}
        data_dict = {'id': id}
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
            c.pkg = context['package']
            c.package_activity_stream = get_action(
                    'package_activity_list_html')(context,
                            {'id': c.pkg_dict['id']})
            c.related_count = c.pkg.related_count
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read dataset %s') % id)

        return render('package/activity.html')

    def resource_embedded_dataviewer(self, id, resource_id,
                                     width=500, height=500):
        """
        Embedded page for a read-only resource dataview. Allows
        for width and height to be specified as part of the
        querystring (as well as accepting them via routes).
        """
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'auth_user_obj': c.userobj}

        try:
            c.resource = get_action('resource_show')(context,
                                                     {'id': resource_id})
            c.package = get_action('package_show')(context, {'id': id})
            c.resource_json = h.json.dumps(c.resource)

            # double check that the resource belongs to the specified package
            if not c.resource['id'] in [r['id']
                                        for r in c.package['resources']]:
                raise NotFound

        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % id)

        # Construct the recline state
        state_version = int(request.params.get('state_version', '1'))
        recline_state = self._parse_recline_state(request.params)
        if recline_state is None:
            abort(400, ('"state" parameter must be a valid recline '
                        'state (version %d)' % state_version))

        c.recline_state = h.json.dumps(recline_state)

        c.width = max(int(request.params.get('width', width)), 100)
        c.height = max(int(request.params.get('height', height)), 100)
        c.embedded = True

        return render('package/resource_embedded_dataviewer.html')

    def _parse_recline_state(self, params):
        state_version = int(request.params.get('state_version', '1'))
        if state_version != 1:
            return None

        recline_state = {}
        for k, v in request.params.items():
            try:
                v = h.json.loads(v)
            except ValueError:
                pass
            recline_state[k] = v

        recline_state.pop('width', None)
        recline_state.pop('height', None)
        recline_state['readOnly'] = True

        # previous versions of recline setup used elasticsearch_url attribute
        # for data api url - see http://trac.ckan.org/ticket/2639
        # fix by relocating this to url attribute which is the default location
        if 'dataset' in recline_state and 'elasticsearch_url' in recline_state['dataset']:
            recline_state['dataset']['url'] = recline_state['dataset']['elasticsearch_url']

        # Ensure only the currentView is available
        # default to grid view if none specified
        if not recline_state.get('currentView', None):
            recline_state['currentView'] = 'grid'
        for k in recline_state.keys():
            if k.startswith('view-') and \
                    not k.endswith(recline_state['currentView']):
                recline_state.pop(k)
        return recline_state

    def resource_views(self, id, resource_id):
        package_type = self._get_package_type(id.split('@')[0])
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj}
        data_dict = {'id': id}

        try:
            check_access('package_update', context, data_dict)
        except NotAuthorized:
            abort(401, _('User %r not authorized to edit %s') % (c.user, id))
        # check if package exists
        try:
            c.pkg_dict = get_action('package_show')(context, data_dict)
            c.pkg = context['package']
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read dataset %s') % id)

        try:
            c.resource = get_action('resource_show')(context,
                                                     {'id': resource_id})
            c.views = get_action('resource_view_list')(context,
                                                       {'id': resource_id})

        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % id)

        self._setup_template_variables(context, {'id': id},
                                       package_type=package_type)

        return render('package/resource_views.html')

    def edit_view(self, id, resource_id, view_id=None):
        package_type = self._get_package_type(id.split('@')[0])
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'for_view': True,
                   'auth_user_obj': c.userobj}

        # update resource should tell us early if the user has privilages.
        try:
            check_access('resource_update', context, {'id': resource_id})
        except NotAuthorized, e:
            abort(401, _('User %r not authorized to edit %s') % (c.user, id))

        # get resource and package data
        try:
            c.pkg_dict = get_action('package_show')(context, {'id': id})
            c.pkg = context['package']
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read dataset %s') % id)
        try:
            c.resource = get_action('resource_show')(context,
                                                     {'id': resource_id})
        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % id)

        data = {}
        errors = {}
        error_summary = {}
        view_type = None
        to_preview = False

        if request.method == 'POST':
            request.POST.pop('save', None)
            to_preview = request.POST.pop('preview', False)
            if to_preview:
                context['preview'] = True
            to_delete = request.POST.pop('delete', None)
            data = clean_dict(dict_fns.unflatten(tuplize_dict(parse_params(
                request.params, ignore_keys=CACHE_PARAMETERS))))
            data['resource_id'] = resource_id

            try:
                if to_delete:
                    data['id'] = view_id
                    get_action('resource_view_delete')(context, data)
                elif view_id:
                    data['id'] = view_id
                    data = get_action('resource_view_update')(context, data)
                else:
                    data = get_action('resource_view_create')(context, data)
            except ValidationError, e:
                ## Could break preview if validation error
                to_preview = False
                errors = e.error_dict
                error_summary = e.error_summary
            except NotAuthorized:
                ## This should never happen unless the user maliciously changed
                ## the resource_id in the url.
                abort(401, _('Unauthorized to edit resource'))
            else:
                if not to_preview:
                    redirect(h.url_for(controller='package',
                                       action='resource_views',
                                       id=id, resource_id=resource_id))

        ## view_id exists only when updating
        if view_id:
            try:
                old_data = get_action('resource_view_show')(context,
                                                            {'id': view_id})
                data = data or old_data
                view_type = old_data.get('view_type')
                ## might as well preview when loading good existing view
                if not errors:
                    to_preview = True
            except NotFound:
                abort(404, _('View not found'))
            except NotAuthorized:
                abort(401, _('Unauthorized to view View %s') % view_id)

        view_type = view_type or request.GET.get('view_type')
        data['view_type'] = view_type
        view_plugin = datapreview.get_view_plugin(view_type)
        if not view_plugin:
            abort(404, _('View Type Not found'))

        self._setup_template_variables(context, {'id': id},
                                       package_type=package_type)

        data_dict = {'package': c.pkg_dict, 'resource': c.resource,
                     'resource_view': data}

        view_template = view_plugin.view_template(context, data_dict)
        form_template = view_plugin.form_template(context, data_dict)

        vars = {'form_template': form_template,
                'view_template': view_template,
                'data': data,
                'errors': errors,
                'error_summary': error_summary,
                'to_preview': to_preview}
        vars.update(
            view_plugin.setup_template_variables(context, data_dict) or {})
        vars.update(data_dict)

        if view_id:
            return render('package/edit_view.html', extra_vars=vars)

        return render('package/new_view.html', extra_vars=vars)

    def resource_view(self, id, resource_id, view_id=None):
        '''
        Embedded page for a resource view.

        Depending on the type, different views are loaded. This could be an
        img tag where the image is loaded directly or an iframe that embeds a
        webpage, recline or a pdf preview.
        '''
        context = {'model': model,
                   'session': model.Session,
                   'user': c.user or c.author,
                   'auth_user_obj': c.userobj}

        try:
            package = get_action('package_show')(context, {'id': id})
        except NotFound:
            abort(404, _('Dataset not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read dataset %s') % id)

        try:
            resource = get_action('resource_show')(
                context, {'id': resource_id})
        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % resource_id)

        view = None
        if request.params.get('resource_view', ''):
            try:
                view = json.loads(request.params.get('resource_view', ''))
            except ValueError:
                abort(409, _('Bad resource view data'))
        elif view_id:
            try:
                view = get_action('resource_view_show')(
                    context, {'id': view_id})
            except NotFound:
                abort(404, _('Resource view not found'))
            except NotAuthorized:
                abort(401,
                      _('Unauthorized to read resource view %s') % view_id)

        if not view or not isinstance(view, dict):
            abort(404, _('Resource view not supplied'))

        return h.rendered_resource_view(view, resource, package, embed=True)

    def resource_datapreview(self, id, resource_id):
        '''
        Embedded page for a resource data-preview.

        Depending on the type, different previews are loaded.  This could be an
        img tag where the image is loaded directly or an iframe that embeds a
        webpage, recline or a pdf preview.
        '''
        context = {
            'model': model,
            'session': model.Session,
            'user': c.user or c.author,
            'auth_user_obj': c.userobj
        }

        try:
            c.resource = get_action('resource_show')(context,
                                                     {'id': resource_id})
            c.package = get_action('package_show')(context, {'id': id})

            data_dict = {'resource': c.resource, 'package': c.package}

            preview_plugin = datapreview.get_preview_plugin(data_dict)

            if preview_plugin is None:
                abort(409, _('No preview has been defined.'))

            preview_plugin.setup_template_variables(context, data_dict)
            c.resource_json = json.dumps(c.resource)
        except NotFound:
            abort(404, _('Resource not found'))
        except NotAuthorized:
            abort(401, _('Unauthorized to read resource %s') % id)
        else:
            return render(preview_plugin.preview_template(context, data_dict))
