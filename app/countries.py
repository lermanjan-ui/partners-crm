"""Страна контакта одним видом: из поля «Страна», иначе из адреса, иначе по коду телефона (с пометкой)."""
import re

# ISO-код → (название по-русски, другие написания: английские, местные, сокращения, крупные города)
COUNTRIES: dict[str, tuple[str, list[str]]] = {
    "UZ": ("Узбекистан", ["uzbekistan", "o'zbekiston", "ozbekiston", "узбекистан", "tashkent", "ташкент", "samarkand", "самарканд"]),
    "KZ": ("Казахстан", ["kazakhstan", "казахстан", "almaty", "алматы", "astana", "астана"]),
    "KG": ("Кыргызстан", ["kyrgyzstan", "kyrgyz republic", "кыргызстан", "киргизия", "bishkek", "бишкек"]),
    "TJ": ("Таджикистан", ["tajikistan", "таджикистан", "dushanbe", "душанбе"]),
    "TM": ("Туркменистан", ["turkmenistan", "туркменистан", "ashgabat"]),
    "AZ": ("Азербайджан", ["azerbaijan", "азербайджан", "baku", "баку"]),
    "GE": ("Грузия", ["georgia (country)", "грузия", "sakartvelo", "tbilisi", "тбилиси"]),
    "AM": ("Армения", ["armenia", "армения", "yerevan", "ереван"]),
    "RU": ("Россия", ["russia", "russian federation", "россия", "рф", "moscow", "москва", "saint petersburg", "санкт-петербург"]),
    "BY": ("Беларусь", ["belarus", "беларусь", "белоруссия", "minsk", "минск"]),
    "UA": ("Украина", ["ukraine", "украина", "kyiv", "kiev", "киев"]),
    "MD": ("Молдова", ["moldova", "молдова"]),
    "GB": ("Великобритания", ["united kingdom", "uk", "u.k.", "great britain", "britain", "england", "scotland", "wales",
                              "великобритания", "англия", "соединенное королевство", "соединённое королевство",
                              "london", "лондон", "manchester", "edinburgh"]),
    "IE": ("Ирландия", ["ireland", "éire", "ирландия", "dublin", "дублин", "cork"]),
    "PT": ("Португалия", ["portugal", "португалия", "lisbon", "lisboa", "лиссабон", "porto", "maia", "braga"]),
    "ES": ("Испания", ["spain", "españa", "espana", "испания", "madrid", "мадрид", "barcelona", "барселона", "valencia"]),
    "FR": ("Франция", ["france", "франция", "paris", "париж", "lyon"]),
    "DE": ("Германия", ["germany", "deutschland", "германия", "berlin", "берлин", "munich", "münchen", "hamburg", "frankfurt"]),
    "IT": ("Италия", ["italy", "italia", "италия", "rome", "roma", "milan", "milano", "милан", "рим"]),
    "NL": ("Нидерланды", ["netherlands", "the netherlands", "holland", "nederland", "нидерланды", "голландия", "amsterdam", "амстердам", "rotterdam"]),
    "BE": ("Бельгия", ["belgium", "belgique", "бельгия", "brussels", "bruxelles", "брюссель"]),
    "LU": ("Люксембург", ["luxembourg", "люксембург"]),
    "CH": ("Швейцария", ["switzerland", "schweiz", "suisse", "швейцария", "zurich", "zürich", "geneva", "genève", "цюрих", "женева"]),
    "AT": ("Австрия", ["austria", "österreich", "австрия", "vienna", "wien", "вена"]),
    "PL": ("Польша", ["poland", "polska", "польша", "warsaw", "warszawa", "варшава", "krakow", "kraków"]),
    "CZ": ("Чехия", ["czech republic", "czechia", "чехия", "prague", "praha", "прага"]),
    "SK": ("Словакия", ["slovakia", "словакия", "bratislava"]),
    "HU": ("Венгрия", ["hungary", "венгрия", "budapest", "будапешт"]),
    "RO": ("Румыния", ["romania", "румыния", "bucharest"]),
    "BG": ("Болгария", ["bulgaria", "болгария", "sofia"]),
    "GR": ("Греция", ["greece", "греция", "athens", "афины"]),
    "CY": ("Кипр", ["cyprus", "кипр", "limassol", "лимассол", "nicosia"]),
    "MT": ("Мальта", ["malta", "мальта"]),
    "HR": ("Хорватия", ["croatia", "хорватия", "zagreb"]),
    "SI": ("Словения", ["slovenia", "словения", "ljubljana"]),
    "RS": ("Сербия", ["serbia", "сербия", "belgrade", "белград"]),
    "DK": ("Дания", ["denmark", "дания", "copenhagen", "копенгаген"]),
    "SE": ("Швеция", ["sweden", "швеция", "stockholm", "стокгольм"]),
    "NO": ("Норвегия", ["norway", "норвегия", "oslo", "осло"]),
    "FI": ("Финляндия", ["finland", "финляндия", "helsinki", "хельсинки"]),
    "IS": ("Исландия", ["iceland", "исландия"]),
    "EE": ("Эстония", ["estonia", "эстония", "tallinn", "таллин"]),
    "LV": ("Латвия", ["latvia", "латвия", "riga", "рига"]),
    "LT": ("Литва", ["lithuania", "литва", "vilnius", "вильнюс"]),
    "TR": ("Турция", ["turkey", "türkiye", "turkiye", "турция", "istanbul", "стамбул", "ankara", "анкара"]),
    "IL": ("Израиль", ["israel", "израиль", "tel aviv", "тель-авив", "jerusalem"]),
    "AE": ("ОАЭ", ["united arab emirates", "uae", "u.a.e.", "оаэ", "эмираты", "dubai", "дубай", "abu dhabi", "абу-даби"]),
    "SA": ("Саудовская Аравия", ["saudi arabia", "ksa", "саудовская аравия", "riyadh", "эр-рияд", "jeddah"]),
    "QA": ("Катар", ["qatar", "катар", "doha", "доха"]),
    "KW": ("Кувейт", ["kuwait", "кувейт"]),
    "BH": ("Бахрейн", ["bahrain", "бахрейн"]),
    "OM": ("Оман", ["oman", "оман", "muscat"]),
    "JO": ("Иордания", ["jordan", "иордания", "amman"]),
    "LB": ("Ливан", ["lebanon", "ливан", "beirut"]),
    "EG": ("Египет", ["egypt", "египет", "cairo", "каир"]),
    "MA": ("Марокко", ["morocco", "марокко", "casablanca"]),
    "TN": ("Тунис", ["tunisia", "тунис"]),
    "DZ": ("Алжир", ["algeria", "алжир"]),
    "NG": ("Нигерия", ["nigeria", "нигерия", "lagos"]),
    "KE": ("Кения", ["kenya", "кения", "nairobi"]),
    "ZA": ("ЮАР", ["south africa", "юар", "cape town", "johannesburg"]),
    "IR": ("Иран", ["iran", "иран", "tehran"]),
    "IQ": ("Ирак", ["iraq", "ирак"]),
    "AF": ("Афганистан", ["afghanistan", "афганистан"]),
    "PK": ("Пакистан", ["pakistan", "пакистан", "karachi", "lahore", "islamabad"]),
    "IN": ("Индия", ["india", "индия", "bangalore", "bengaluru", "mumbai", "delhi", "new delhi"]),
    "BD": ("Бангладеш", ["bangladesh", "бангладеш", "dhaka"]),
    "LK": ("Шри-Ланка", ["sri lanka", "шри-ланка"]),
    "NP": ("Непал", ["nepal", "непал"]),
    "CN": ("Китай", ["china", "prc", "китай", "beijing", "пекин", "shanghai", "шанхай", "shenzhen"]),
    "HK": ("Гонконг", ["hong kong", "гонконг"]),
    "TW": ("Тайвань", ["taiwan", "тайвань", "taipei"]),
    "JP": ("Япония", ["japan", "япония", "tokyo", "токио"]),
    "KR": ("Южная Корея", ["south korea", "korea", "republic of korea", "южная корея", "корея", "seoul", "сеул"]),
    "SG": ("Сингапур", ["singapore", "сингапур"]),
    "MY": ("Малайзия", ["malaysia", "малайзия", "kuala lumpur"]),
    "ID": ("Индонезия", ["indonesia", "индонезия", "jakarta", "bali"]),
    "TH": ("Таиланд", ["thailand", "таиланд", "bangkok", "бангкок"]),
    "VN": ("Вьетнам", ["vietnam", "viet nam", "вьетнам", "hanoi", "ho chi minh"]),
    "PH": ("Филиппины", ["philippines", "филиппины", "manila"]),
    "AU": ("Австралия", ["australia", "австралия", "sydney", "сидней", "melbourne"]),
    "NZ": ("Новая Зеландия", ["new zealand", "новая зеландия", "auckland"]),
    "US": ("США", ["united states", "united states of america", "usa", "u.s.a.", "u.s.", "us", "сша", "америка",
                   "new york", "нью-йорк", "san francisco", "сан-франциско", "los angeles", "miami", "boston", "chicago",
                   "seattle", "austin", "california", "texas", "florida"]),
    "CA": ("Канада", ["canada", "канада", "toronto", "торонто", "vancouver", "ванкувер", "montreal", "montréal", "монреаль"]),
    "MX": ("Мексика", ["mexico", "méxico", "мексика"]),
    "BR": ("Бразилия", ["brazil", "brasil", "бразилия", "são paulo", "sao paulo", "rio de janeiro"]),
    "AR": ("Аргентина", ["argentina", "аргентина", "buenos aires"]),
    "CL": ("Чили", ["chile", "чили", "santiago"]),
    "CO": ("Колумбия", ["colombia", "колумбия", "bogota", "bogotá", "medellin"]),
    "PE": ("Перу", ["peru", "perú", "перу", "lima"]),
}
NA = "NA"  # +1: США или Канада — по номеру не различить
NAMES = {code: name for code, (name, _) in COUNTRIES.items()} | {NA: "США/Канада"}

# Коды телефонов → страна (самое длинное совпадение побеждает)
PHONE_CODES = {
    "1": NA, "20": "EG", "27": "ZA", "30": "GR", "31": "NL", "32": "BE", "33": "FR", "34": "ES", "36": "HU", "39": "IT",
    "40": "RO", "41": "CH", "43": "AT", "44": "GB", "45": "DK", "46": "SE", "47": "NO", "48": "PL", "49": "DE",
    "51": "PE", "52": "MX", "54": "AR", "55": "BR", "56": "CL", "57": "CO", "60": "MY", "61": "AU", "62": "ID",
    "63": "PH", "64": "NZ", "65": "SG", "66": "TH", "7": "RU", "76": "KZ", "77": "KZ", "81": "JP", "82": "KR",
    "84": "VN", "86": "CN", "90": "TR", "91": "IN", "92": "PK", "93": "AF", "94": "LK", "98": "IR", "212": "MA",
    "213": "DZ", "216": "TN", "234": "NG", "254": "KE", "351": "PT", "352": "LU", "353": "IE", "354": "IS",
    "356": "MT", "357": "CY", "358": "FI", "359": "BG", "370": "LT", "371": "LV", "372": "EE", "373": "MD",
    "374": "AM", "375": "BY", "380": "UA", "381": "RS", "385": "HR", "386": "SI", "420": "CZ", "421": "SK",
    "852": "HK", "880": "BD", "886": "TW", "961": "LB", "962": "JO", "964": "IQ", "965": "KW", "966": "SA",
    "968": "OM", "971": "AE", "972": "IL", "973": "BH", "974": "QA", "977": "NP", "992": "TJ", "993": "TM",
    "994": "AZ", "995": "GE", "996": "KG", "998": "UZ",
}


def flag(code: str) -> str:
    if code == NA:
        return "🇺🇸🇨🇦"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code) if len(code) == 2 else ""


def label(code: str | None) -> str:
    return f"{flag(code)} {NAMES[code]}" if code in NAMES else ""


def _norm(text: str) -> str:
    return " " + re.sub(r"[^\w\s.'\-]+", " ", (text or "").lower()).replace("ё", "е") + " "


_ALIAS_CODE = {a.replace("ё", "е"): code for code, (name, aliases) in COUNTRIES.items() for a in [name.lower(), *aliases]}
# Одно выражение на все названия: длинные раньше коротких («south korea» раньше «korea»), только целыми словами
_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(a) for a in sorted(_ALIAS_CODE, key=len, reverse=True)) + r")(?![\w])")


def from_text(text: str | None) -> str | None:
    """Ищет название страны (или крупный город) в тексте; длинные названия — раньше коротких."""
    if not text:
        return None
    m = _RE.search(_norm(text))
    return _ALIAS_CODE[m.group(1)] if m else None


def from_phone(phone: str | None) -> str | None:
    if not phone or not phone.strip().startswith("+"):
        return None
    digits = re.sub(r"\D", "", phone)
    for n in (3, 2, 1):
        if digits[:n] in PHONE_CODES:
            return PHONE_CODES[digits[:n]]
    return None


def resolve(card) -> tuple[str | None, str | None]:
    """(код страны, откуда взято: country | address | phone)."""
    code = from_text(card.country)
    if code:
        return code, "country"
    code = from_text(card.address)
    if code:
        return code, "address"
    for p in card.phones or []:
        code = from_phone(p)
        if code:
            return code, "phone"
    for v in card.socials or []:  # «WhatsApp: +44…»
        if "+" in v:
            code = from_phone(v[v.index("+"):])
            if code:
                return code, "phone"
    return None, None
