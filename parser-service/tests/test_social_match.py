from app.adapters import contacts
from app.adapters.google_maps import _address_locality, _search_queries
from app.adapters.social_match import OrgCard, address_matches, match, name_matches, phone_matches

# Данные — как вживую 2026-10-04: карточка Google "Naru" и три Instagram-профиля из поиска.
NARU = OrgCard(
    names=["Naru", "Ресторан Нару"],
    phone_digits="442073797962",
    address="230 Shaftesbury Ave, London WC2H 8EG, Великобритания",
)
REAL = (
    "naru.restaurant.uk | Naru | 464 posts | Restaurant | Authentic Korean Restaurant | "
    "📞For reservation call 02073797962 | 230 Shaftesbury Ave, London, United Kingdom WC2H 8EG"
)
SAME_ADDRESS = "narurestaurantlndn | Naru London | Restaurant | 🇰🇷Korean restaurant 🇰🇷 | 230 Shaftesbury Ave, London, United Kingdom WC2H 8EG"
OTHER_PLACE = (
    "korean.restaurantlondonnaru | Korean Restaurant London Naru | Exclusive Family run Korean Restaurant "
    "in the Heart of Greenwich London. | 📍3 Stockwell street SE10 9JN London."
)


def test_real_profile_matches_by_name_phone_and_address():
    result = match(NARU, "naru.restaurant.uk", "Naru", REAL)
    assert result.ok and result.phone and result.address
    assert result.reasons == ["название", "телефон", "адрес"]


def test_same_address_without_phone_still_matches_but_scores_lower():
    result = match(NARU, "narurestaurantlndn", "Naru London", SAME_ADDRESS)
    assert result.ok and not result.phone
    assert result.score < match(NARU, "naru.restaurant.uk", "Naru", REAL).score


def test_namesake_at_other_address_rejected():
    result = match(NARU, "korean.restaurantlondonnaru", "Korean Restaurant London Naru", OTHER_PLACE)
    assert result.name and not result.ok


def test_name_alone_is_not_enough():
    assert not match(NARU, "naru.london", "Naru", "Best sushi in town").ok


def test_unrelated_name_rejected():
    assert not name_matches(NARU.names, "larina.natusik.36", "")


def test_phone_formats():
    assert phone_matches("74956260282", "Звоните: 8 (495) 626-02-82")
    assert phone_matches("442073797962", "+44 20 7379 7962")
    assert not phone_matches("442073797962", "call 02073797000")


def test_russian_address():
    assert address_matches("ул. Баумана, 44/8, Казань, Респ. Татарстан, 420111", "Казань, Баумана 44")
    assert not address_matches("ул. Баумана, 44/8, Казань", "Казань, Баумана 12")


def test_queries_use_original_name_and_latin_city():
    assert _address_locality(NARU.address) == "London"
    assert _search_queries(NARU, "Лондон") == ["Naru London", "Naru", "Ресторан Нару Лондон"]


def test_instagram_links():
    assert contacts.instagram_lead("https://instagram.com/naru.restaurant.uk?igshid=YTQw").external_id == "naru.restaurant.uk"
    assert contacts.instagram_lead("https://www.instagram.com/p/Cxyz/") is None
    # Инстаграм в поле "сайт" карточки — это не сайт, а канал связи.
    assert not contacts.is_external_site("https://instagram.com/naru.restaurant.uk")
    assert not contacts.is_external_site("https://linktr.ee/naru")
