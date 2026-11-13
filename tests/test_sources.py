import json
from sources import parse_kufar, parse_onliner, parse_realt, permitted_url, coordinates, _next_data


def test_onliner_coordinates_and_price():
    payload = {'apartments': [
        {'id': 55, 'rent_type': '2_rooms', 'price': {'amount': '600.00', 'currency': 'USD',
         'converted': {'USD': {'amount': '600.00', 'currency': 'USD'}}},
         'location': {'user_address': 'Мележа, 5к1', 'latitude': 53.940579, 'longitude': 27.597341},
         'contact': {'owner': True}, 'url': 'https://ak.onliner.by/apartments/55'},
        {'id': 56, 'rent_type': 'room', 'url': 'https://ak.onliner.by/apartments/56'},
    ]}
    ads = parse_onliner(payload)
    assert len(ads) == 1
    assert ads[0].rooms == 2
    assert ads[0].price == 600
    assert ads[0].owner is True
    assert (ads[0].lat, ads[0].lon) == (53.940579, 27.597341)


def test_kufar_long_rent_example():
    payload = {'ads': [{
        'ad_id': 123, 'ad_link': 'https://re.kufar.by/vi/minsk/snyat/kvartiru/123',
        'subject': 'Сдаётся двухкомнатная квартира', 'price_usd': '65000',
        'ad_parameters': [
            {'p': 'address', 'vl': 'ул. Мележа, 5к1'},
            {'p': 'rooms', 'vl': '2'},
        ], 'company_ad': False,
    }]}
    ads = parse_kufar(payload)
    assert len(ads) == 1
    assert ads[0].price == 650
    assert ads[0].rooms == 2
    assert ads[0].address == 'ул. Мележа, 5к1'
    assert ads[0].owner is None  # non-company != verified owner


def test_realt_next_data_and_geocoordinates():
    data = {'props': {'pageProps': {'objects': [{
        'code': '1234', 'title': 'Квартира в Минске', 'townName': 'Минск',
        'address': 'ул. Мележа, 5', 'priceCurrency': 840, 'price': 470,
        'rooms': 1, 'geo': {'coordinates': [27.60, 53.94]},
    }]}}}
    html = '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script></html>'
    parsed = parse_realt(_next_data(html))
    assert len(parsed) == 1
    assert parsed[0].price == 470
    assert (parsed[0].lat, parsed[0].lon) == (53.94, 27.60)
    assert parsed[0].url.endswith('/rent-flat-for-long/object/1234/')


def test_secure_url_and_coords():
    assert permitted_url('https://realt.by.evil.com/ads/1', 'Realt') is None
    assert permitted_url('javascript:alert(1)', 'Onliner') is None
    assert coordinates({'location': {'latitude': 53.94, 'longitude': 27.59}}) == (53.94, 27.59)
    assert coordinates({'coordinates': [27.59, 53.94]}) == (53.94, 27.59)
    assert coordinates({'lat': 27.59, 'lon': 53.94}) == (53.94, 27.59)
    assert coordinates({'lat': 10, 'lon': 12}) is None
