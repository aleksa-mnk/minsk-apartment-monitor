from addresses import address_key, build_osm_index, house_key, street_key
from store import Store


def test_address_normalization():
    assert address_key('Минск, ул. Мележа, 5, корп. 1') == ('мележа', '5к1')
    assert address_key('г. Минск, Мележа ул., 5/1') == ('мележа', '5к1')
    assert street_key('улица Мележа') == street_key('Мележа ул.')
    assert address_key('Минск') is None
    assert address_key('Мележа') is None


def test_overpass_import_and_lookup():
    payload = {'elements': [
        {'type': 'way', 'tags': {'addr:street': 'улица Мележа', 'addr:housenumber': '5/1'},
         'center': {'lat': 53.940579, 'lon': 27.597341}},
        {'type': 'node', 'tags': {'addr:street': 'улица Мележа', 'addr:housenumber': '5/2'},
         'lat': 53.9406, 'lon': 27.5973},
        {'type': 'node', 'tags': {'addr:street': 'Другая', 'addr:housenumber': '1'},
         'lat': 33, 'lon': 44},
    ]}
    index = build_osm_index(payload)
    assert len(index) == 2
    store = Store(':memory:')
    store.replace_addresses(index)
    assert store.lookup_address('мележа|5к1') == (53.940579, 27.597341)
    assert store.address_count() == 2
    store.close()
