"""HeroSMS (SMS-Activate) country id -> ISO alpha-2 -> flag emoji.

IDs cross-checked against the live getCountries response (194 countries).
"""
from __future__ import annotations

COUNTRY_ISO: dict[str, str] = {
    "1": "ua", "2": "kz", "3": "cn", "4": "ph", "5": "mm", "6": "id", "7": "my",
    "8": "ke", "9": "tz", "10": "vn", "11": "kg", "13": "il", "14": "hk", "15": "pl",
    "16": "gb", "17": "mg", "18": "cd", "19": "ng", "20": "mo", "21": "eg", "22": "in",
    "23": "ie", "24": "kh", "25": "la", "26": "ht", "27": "ci", "28": "gm", "29": "rs",
    "30": "ye", "31": "za", "32": "ro", "33": "co", "34": "ee", "35": "az", "36": "ca",
    "37": "ma", "38": "gh", "39": "ar", "40": "uz", "41": "cm", "42": "td", "43": "de",
    "44": "lt", "45": "hr", "46": "se", "47": "iq", "48": "nl", "49": "lv", "50": "at",
    "51": "by", "52": "th", "53": "sa", "54": "mx", "55": "tw", "56": "es", "57": "ir",
    "58": "dz", "59": "si", "60": "bd", "61": "sn", "62": "tr", "63": "cz", "64": "lk",
    "65": "pe", "66": "pk", "67": "nz", "68": "gn", "69": "ml", "70": "ve", "71": "et",
    "72": "mn", "73": "br", "74": "af", "75": "ug", "76": "ao", "77": "cy", "78": "fr",
    "79": "pg", "80": "mz", "81": "np", "82": "be", "83": "bg", "84": "hu", "85": "md",
    "86": "it", "87": "py", "88": "hn", "89": "tn", "90": "ni", "91": "tl", "92": "bo",
    "93": "cr", "94": "gt", "95": "ae", "96": "zw", "97": "pr", "98": "sd", "99": "tg",
    "100": "kw", "101": "sv", "102": "ly", "103": "jm", "104": "tt", "105": "ec",
    "106": "sz", "107": "om", "108": "ba", "109": "do", "110": "sy", "111": "qa",
    "112": "pa", "113": "cu", "114": "mr", "115": "sl", "116": "jo", "117": "pt",
    "118": "bb", "119": "bi", "120": "bj", "121": "bn", "122": "bs", "123": "bw",
    "124": "bz", "125": "cf", "126": "dm", "127": "gd", "128": "ge", "129": "gr",
    "130": "gw", "131": "gy", "132": "is", "133": "km", "134": "kn", "135": "lr",
    "136": "ls", "137": "mw", "138": "na", "139": "ne", "140": "rw", "141": "sk",
    "142": "sr", "143": "tj", "144": "mc", "145": "bh", "146": "re", "147": "zm",
    "148": "am", "149": "so", "150": "cg", "151": "cl", "152": "bf", "153": "lb",
    "154": "ga", "155": "al", "156": "uy", "157": "mu", "158": "bt", "159": "mv",
    "160": "gp", "161": "tm", "162": "gf", "163": "fi", "164": "lc", "165": "lu",
    "166": "vc", "167": "gq", "168": "dj", "169": "ag", "170": "ky", "171": "me",
    "172": "dk", "173": "ch", "174": "no", "175": "au", "176": "er", "177": "ss",
    "178": "st", "179": "aw", "180": "ms", "181": "ai", "182": "jp", "183": "mk",
    "184": "sc", "185": "nc", "186": "cv", "187": "us", "188": "ps", "189": "fj",
    "196": "sg", "198": "ws", "199": "mt", "201": "gi", "203": "xk", "204": "nu",
}


def flag(country_id) -> str:
    """Flag emoji for a HeroSMS country id, or a neutral flag if unknown."""
    iso = COUNTRY_ISO.get(str(country_id))
    if not iso or len(iso) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - 97) for c in iso.lower())


def iso_flag(code) -> str:
    """Flag emoji for a 2-letter ISO country code (e.g. 'US' -> 🇺🇸).

    eSIM Access uses ISO-2 codes directly. Multi-country/region packages use
    non-ISO codes (e.g. region groupings) — show a globe for those.
    """
    code = str(code or "").strip()
    if len(code) != 2 or not code.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - 97) for c in code.lower())
