import datetime
import json
import operator
import pickle
from functools import reduce

from django.contrib.contenttypes.models import ContentType
from django.http.request import QueryDict

from apps.configs.api.serializers import UnitSerializer
from apps.configs.models import (
    Config,
    AbstractProduct,
    Source,
    Type,
    Chart,
)
from apps.dailytrans.utils import (
    get_daily_price_volume,
    get_daily_price_by_year,
    get_monthly_price_distribution,
    get_integration,
)
from apps.dailytrans.utils import to_date
from apps.events.forms import EventForm
from apps.watchlists.api.serializers import (
    MonitorProfileSerializer,
    WatchlistSerializer,
)
from apps.watchlists.models import (
    Watchlist,
    MonitorProfile,
)
from dashboard.caches import redis_instance as cache

CONTENT_TYPE_CONFIG_CHARTS_CACHE_KEY = "content_type_config{config_id}_charts"
CONTENT_TYPE_PRODUCT_CHARTS_CACHE_KEY = "content_type_product{product_id}_charts"


def jarvismenu_extra_context(view):
    """
    A function return extra context work for JarvisMenu CBV and other view with arguments wi, ct, oi, lct, loi

    wi(watchlist id) allows: integer
    ct(content type) allows: 'config', 'type', 'product', 'source'
    oi(object id) allows: integer
    lct(last content type) allows: 'config', 'type', 'product', 'source'
    loi(last object id) allows: integer
    """
    kwargs = view.kwargs
    user = view.request.user
    extra_context = dict()
    watchlist_id = kwargs.get('wi')
    content_type = kwargs.get('ct')
    object_id = kwargs.get('oi')
    last_content_type = kwargs.get('lct')
    last_object_id = kwargs.get('loi')
    watchlist = Watchlist.objects.get(id=watchlist_id)
    extra_context['watchlist'] = watchlist

    # 品項第一層(品項分類(Config))
    if content_type == 'config':
        config = Config.objects.get(id=object_id)
        products = config.first_level_products(watchlist=watchlist)

        if products:
            extra_context['items'] = products
            extra_context['ct'] = 'abstractproduct'
            extra_context['lct'] = 'config'
            extra_context['loi'] = object_id

    # TODO: 目前看起來這個條件不會進入
    elif content_type == 'type':
        if last_content_type == 'abstractproduct':
            product = AbstractProduct.objects.get(id=last_object_id)

            if product.has_child:
                extra_context['items'] = product.children(watchlist=watchlist).filter(type__id=object_id)
                extra_context['ct'] = 'abstractproduct'
                extra_context['lct'] = 'type'
                extra_context['loi'] = object_id

            elif product.has_source:

                extra_context['items'] = product.sources(watchlist=watchlist)
                extra_context['ct'] = 'source'
                extra_context['lct'] = 'abstractproduct'
                extra_context['loi'] = product.id

    # 品項第二層以後
    elif content_type == 'abstractproduct':
        product = AbstractProduct.objects.get(id=object_id)
        extra_context['lct'] = 'abstractproduct'
        extra_context['loi'] = product.id
        children_has_monitor_profile = MonitorProfile.objects.filter(
            watchlist=watchlist,
            product__id__in=product.children_all().values_list('id', flat=True)
        )

        # TODO: 目前看起來這個條件不會進入
        if product.level >= product.config.type_level and not user.info.menu_viewer and not children_has_monitor_profile:
            pass

        # TODO: 目前看起來這個條件不會進入
        elif product.types(watchlist=watchlist).count() > 1 and product.level == product.config.type_level:
            extra_context['items'] = product.types(watchlist=watchlist)
            extra_context['ct'] = 'type'

        elif product.has_child:
            extra_context['items'] = product.children(watchlist=watchlist)
            extra_context['ct'] = 'abstractproduct'

        # 最下層品項，再展開則為該品項的來源清單
        elif product.has_source:
            extra_context['items'] = product.sources(watchlist=watchlist)
            extra_context['ct'] = 'source'

    return extra_context


def product_selector_ui_extra_context(view):
    extra_context = dict()

    # Captured values
    kwargs = view.kwargs
    step = int(kwargs.get('step'))

    # Post data
    data = kwargs.get('POST') or QueryDict()
    config_id = data.get('config_id')
    type_id = data.get('type_id')

    extra_context['step'] = step
    if step == 1:
        extra_context['configs'] = Config.objects.order_by('id').all()
    elif step == 2:
        extra_context['types'] = Config.objects.get(id=config_id).types()
    elif step == 3:
        config = Config.objects.get(id=config_id)
        products = config.products().filter(track_item=True, type__id=type_id)

        # Handling special cases, if there is parent product e.g. FB1, replaces with sub products
        if config_id == '5':
            # filter 內新增參數 type_id,原只過濾 code 包含 FB 會讓蔬果產地多了批發品項也會多火鶴花(FB)
            fb_related_products = AbstractProduct.objects.filter(track_item=False, code__icontains='FB', type_id= type_id)
            products = products.exclude(name__icontains='FB') | fb_related_products

        # Handling special cases, replace origin seafoods parent with sub product
        if config_id == '13' and type_id == '2':
            products = config.products().filter(track_item=False, type__id=type_id)

        # Show products parent name and products name or code in select list
        if config_id in ['8', '10', '11', '12'] or config_id == '13' and type_id == '2':
            extra_context['show_parent'] = True

        # Show products parent name and code in select list
        if config_id in ['5', '6', '7', '13'] and type_id == '1':
            extra_context['show_code'] = True

        extra_context['config_id'] = int(config_id)
        extra_context['products'] = products
        extra_context['sources'] = config.source_set.all().filter(type__id=type_id)

    return extra_context


def chart_tab_extra_context(view):
    extra_context = dict()

    # Query params
    params = view.request.GET
    config_id = params.get('config')
    type_id = params.get('type')
    product_ids = params.get('products')  # string with separator
    source_ids = params.get('sources')  # string with separator

    extra_context['charts'] = Config.objects.get(id=config_id).charts.filter(id__in=[1, 2, 3, 4])
    extra_context['type'] = type_id
    extra_context['products'] = '_'.join(product_ids.split(',')) if product_ids else '_'
    extra_context['sources'] = '_'.join(source_ids.split(',')) if source_ids else '_'

    return extra_context


def watchlist_base_chart_tab_extra_context(view):
    # Captured values
    kwargs = view.kwargs
    content_type = kwargs.get('ct')
    object_id = kwargs.get('oi')
    last_content_type = kwargs.get('lct')
    last_object_id = kwargs.get('loi')
    watchlist_id = kwargs.get('wi')
    watchlist = Watchlist.objects.get(id=watchlist_id)

    extra_context = {'watchlist': watchlist}

    if content_type == 'config':
        config = Config.objects.get(id=object_id)
        cache_key = CONTENT_TYPE_CONFIG_CHARTS_CACHE_KEY.format(config_id=config.id)
        charts = cache.get(cache_key)

        if charts is None:
            charts = config.charts.all()
            cache.set(cache_key, charts, dump=True)
        else:
            charts = pickle.loads(charts)

        extra_context['charts'] = charts

    elif content_type == 'abstractproduct':
        content_type_with_abstract_product(object_id, extra_context, watchlist)

    elif content_type in ['type', 'source']:
        if last_content_type == 'abstractproduct':
            product = AbstractProduct.objects.get(id=last_object_id)
            cache_key = CONTENT_TYPE_PRODUCT_CHARTS_CACHE_KEY.format(product_id=product.id)
            charts = cache.get(cache_key)

            if charts is None:
                charts = product.config.charts.all()
                cache.set(cache_key, charts, dump=True)
            else:
                charts = pickle.loads(charts)

            extra_context['charts'] = charts

    extra_context['watchlists_json'] = WatchlistSerializer(Watchlist.objects.filter(watch_all=False), many=True).data

    return extra_context


def content_type_with_abstract_product(object_id: str, extra_context: dict, watchlist: Watchlist):
    product = AbstractProduct.objects.get(id=object_id)
    cache_key = CONTENT_TYPE_PRODUCT_CHARTS_CACHE_KEY.format(product_id=product.id)
    charts = cache.get(cache_key)

    if charts is None:
        charts = product.config.charts.all()
        cache.set(cache_key, charts, dump=True)
    else:
        charts = pickle.loads(charts)
    extra_context['charts'] = charts
    monitor_profiles = MonitorProfile.objects.filter(product__id=object_id).order_by('price')

    extra_context['product'] = product
    extra_context['types'] = product.types(watchlist=watchlist)

    extra_context['monitor_profiles'] = monitor_profiles
    extra_context['monitor_profiles_json'] = MonitorProfileSerializer(monitor_profiles, many=True).data


def product_selector_base_extra_context(view):
    extra_context = dict()

    # Captured values
    params = view.request.GET
    source_ids = params.get('sources').split('_') if params.get('sources') != '_' else []
    extra_context['sources'] = params.get('sources')

    kwargs = view.kwargs
    chart_id = kwargs.get('ci')
    type_id = kwargs.get('type')
    product_ids = kwargs.get('products').split('_') if kwargs.get('products') != '_' else []

    # Post data
    data = kwargs.get('POST') or QueryDict()
    selected_years = data.getlist('average_years[]')

    _type = Type.objects.get(id=type_id)

    product_qs = AbstractProduct.objects.filter(id__in=product_ids)
    products = product_qs.exclude(track_item=False)
    # Handling special cases, if there is parent product e.g. FB1, replaces with sub products
    if product_qs.filter(track_item=False):
        sub_products = reduce(
            operator.or_,
            (product.children().filter(track_item=True) for product in product_qs.filter(track_item=False))
        )
        products = products | sub_products

    if product_qs.first().track_item is False and product_qs.first().config.id == 13:
        products = product_qs

    if source_ids:
        sources = Source.objects.filter(id__in=source_ids)
    else:
        sources = []

    extra_context['unit_json'] = UnitSerializer(products.first().unit).data

    # get tran data by chart
    series_options = []

    if chart_id in ['1', '2']:

        end_date = datetime.date.today() if chart_id == '1' else None
        start_date = end_date + datetime.timedelta(days=-13) if chart_id == '1' else None
        option = get_daily_price_volume(_type=_type,
                                        items=products,
                                        sources=sources,
                                        start_date=start_date,
                                        end_date=end_date)
        if not option['no_data']:
            series_options.append(option)

    if chart_id == '3':

        option = get_daily_price_by_year(_type=_type,
                                         items=products,
                                         sources=sources)
        if not option['no_data']:
            series_options.append(option)

    if chart_id == '4':
        this_year = datetime.datetime.now().year
        selected_years = selected_years or [y for y in range(this_year-5, this_year)]  # default latest 5 years
        selected_years = [int(y) for y in selected_years]  # cast to integer

        extra_context['method'] = view.request.method
        extra_context['selected_years'] = selected_years

        option = get_monthly_price_distribution(_type=_type,
                                                items=products,
                                                sources=sources,
                                                selected_years=selected_years)
        if not option['no_data']:
            series_options.append(option)

    extra_context['series_options'] = series_options
    extra_context['chart'] = Chart.objects.get(id=chart_id)

    return extra_context


def watchlist_base_chart_contents_extra_context(view):
    extra_context = {}

    # Captured values
    kwargs = view.kwargs
    chart_id = kwargs.get('ci')
    watchlist_id = kwargs.get('wi')
    content_type = kwargs.get('ct')
    object_id = kwargs.get('oi')
    last_content_type = kwargs.get('lct')
    last_object_id = kwargs.get('loi')

    # Post data
    data = kwargs.get('POST') or QueryDict()
    selected_years = data.getlist('average_years[]')
    watchlist = Watchlist.objects.get(id=watchlist_id)

    # selected sources
    sources = None

    # filter items & types
    if content_type == 'config':
        items = watchlist.children().filter(product__config__id=object_id)

    elif content_type == 'type':
        if last_content_type == 'abstractproduct':
            items = watchlist.children().filter_by_product(product__id=last_object_id)

    elif content_type == 'abstractproduct':
        items = watchlist.children().filter_by_product(product__id=object_id)

    elif content_type == 'source':
        items = watchlist.children().filter_by_product(product__id=last_object_id)
        sources = Source.objects.filter(id=object_id)

    types = Type.objects.filter_by_watchlist_items(watchlist_items=items)
    if content_type == 'type':
        types = types.filter(id=object_id)

    extra_context['unit_json'] = UnitSerializer(items.get_unit()).data

    # get tran data by chart
    series_options = []

    if chart_id in ['1', '2', '5']:
        for t in types:
            end_date = datetime.date.today() if chart_id == '1' else None
            start_date = end_date + datetime.timedelta(days=-13) if chart_id == '1' else None
            option = get_daily_price_volume(_type=t,
                                            items=items.filter(product__type=t),
                                            sources=sources,
                                            start_date=start_date,
                                            end_date=end_date)
            if not option['no_data']:
                series_options.append(option)

        if chart_id == '5':
            event_form = EventForm()
            extra_context['event_form'] = event_form
            extra_context['event_form_js'] = [event_form.media.absolute_path(js) for js in event_form.media._js[1:]]
            if content_type in ['config', 'abstractproduct']:
                extra_context['event_content_type_id'] = ContentType.objects.get(model=content_type).id
                extra_context['event_object_id'] = object_id
            elif content_type in ['type', 'source']:
                extra_context['event_content_type_id'] = ContentType.objects.get(model=last_content_type).id
                extra_context['event_object_id'] = last_object_id

    if chart_id == '3':
        for t in types:
            option = get_daily_price_by_year(_type=t,
                                             items=items.filter(product__type=t),
                                             sources=sources)
            if not option['no_data']:
                series_options.append(option)

    if chart_id == '4':
        this_year = datetime.datetime.now().year
        selected_years = selected_years or [y for y in range(this_year-5, this_year)]  # default latest 5 years
        selected_years = [int(y) for y in selected_years]  # cast to integer

        extra_context['method'] = view.request.method
        extra_context['selected_years'] = selected_years

        for t in types:
            option = get_monthly_price_distribution(_type=t,
                                                    items=items.filter(product__type=t),
                                                    sources=sources,
                                                    selected_years=selected_years)
            if not option['no_data']:
                series_options.append(option)

    extra_context['series_options'] = series_options
    extra_context['chart'] = Chart.objects.get(id=chart_id)

    return extra_context


def product_selector_base_integration_extra_context(view):
    extra_context = dict()

    # Captured values
    params = view.request.GET
    source_ids = params.get('sources').split('_') if params.get('sources') != '_' else []

    kwargs = view.kwargs
    chart_id = kwargs.get('ci')
    type_id = kwargs.get('type')
    product_ids = kwargs.get('products').split('_') if kwargs.get('products') != '_' else []

    # Post data
    data = kwargs.get('POST') or QueryDict
    # type_id = data.get('type')
    start_date = to_date(data.get('start_date'))
    end_date = to_date(data.get('end_date'))
    view.to_init = json.loads(data.get('to_init', 'false'))

    _type = Type.objects.get(id=type_id)

    product_qs = AbstractProduct.objects.filter(id__in=product_ids)
    products = product_qs.exclude(track_item=False)
    # Handling special cases, if there is parent product e.g. FB1, replaces with sub products
    if product_qs.filter(track_item=False):
        sub_products = reduce(
            operator.or_,
            (product.children().filter(track_item=True) for product in product_qs.filter(track_item=False))
        )
        products = products | sub_products

    if product_qs.first().track_item is False and product_qs.first().config.id == 13:
        products = product_qs

    if source_ids:
        sources = Source.objects.filter(id__in=source_ids)
    else:
        sources = []

    extra_context['unit_json'] = UnitSerializer(products.first().unit).data

    # get tran data by chart
    series_options = []

    if view.to_init:

        option = get_integration(_type=_type,
                                 items=products,
                                 sources=sources,
                                 start_date=start_date,
                                 end_date=end_date,
                                 to_init=True)
        if not option['no_data']:
            series_options.append(option)

        # datetime format
        formatter = '%m/%d' if start_date.year == end_date.year else '%Y/%m/%d'

        extra_context['start_date_format'] = start_date.strftime(formatter)
        extra_context['end_date_format'] = end_date.strftime(formatter)
        extra_context['chart'] = Chart.objects.get(id=chart_id)

    else:

        option = get_integration(_type=_type,
                                 items=products,
                                 sources=sources,
                                 start_date=start_date,
                                 end_date=end_date,
                                 to_init=False)

        extra_context['option'] = option if not option['no_data'] else None

    extra_context['series_options'] = series_options

    return extra_context


def watchlist_base_integration_extra_context(view):
    kwargs = view.kwargs

    extra_context = dict()

    # Captured values
    chart_id = kwargs.get('ci')
    watchlist_id = kwargs.get('wi')
    content_type = kwargs.get('ct')
    object_id = kwargs.get('oi')
    last_content_type = kwargs.get('lct')
    last_object_id = kwargs.get('loi')

    # Post data
    data = kwargs['POST'] or QueryDict()
    start_date = to_date(data.get('start_date'))
    end_date = to_date(data.get('end_date'))
    type_id = data.get('type')  # required if to_init is True
    view.to_init = json.loads(data.get('to_init', 'false'))

    # get tran data by chart
    series_options = []

    watchlist = Watchlist.objects.get(id=watchlist_id)

    # selected sources
    sources = None

    # filter items & types
    if content_type == 'config':
        items = watchlist.children().filter(product__config__id=object_id)

    elif content_type == 'type':
        if last_content_type == 'abstractproduct':
            items = watchlist.children().filter_by_product(product__id=last_object_id)

    elif content_type == 'abstractproduct':
        items = watchlist.children().filter_by_product(product__id=object_id)

    elif content_type == 'source':
        items = watchlist.children().filter_by_product(product__id=last_object_id)
        sources = Source.objects.filter(id=object_id)

    extra_context['unit_json'] = UnitSerializer(items.get_unit()).data

    if view.to_init:

        types = Type.objects.filter_by_watchlist_items(watchlist_items=items)
        if content_type == 'type':
            types = types.filter(id=object_id)

        for t in types:
            option = get_integration(_type=t,
                                     items=items.filter(product__type=t),
                                     sources=sources,
                                     start_date=start_date,
                                     end_date=end_date,
                                     to_init=True)
            if not option['no_data']:
                series_options.append(option)

        # datetime format
        formatter = '%m/%d' if start_date.year == end_date.year else '%Y/%m/%d'

        extra_context['start_date_format'] = start_date.strftime(formatter)
        extra_context['end_date_format'] = end_date.strftime(formatter)
        extra_context['chart'] = Chart.objects.get(id=chart_id)

    else:
        t = Type.objects.get(id=type_id)

        option = get_integration(_type=t,
                                 items=items.filter(product__type=t),
                                 sources=sources,
                                 start_date=start_date,
                                 end_date=end_date,
                                 to_init=False)

        extra_context['option'] = option if not option['no_data'] else None

    extra_context['series_options'] = series_options
    return extra_context
