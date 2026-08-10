from __future__ import annotations

from ..models import CrawlResult, HreflangReference, Issue, Page
from ..rules import get_rule


_LANGUAGES = set("aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu".split())
_REGIONS = set("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split())


def valid_language_tag(value: str) -> bool:
    if value.lower() == "x-default":
        return True
    parts = value.split("-")
    if not parts or parts.pop(0).lower() not in _LANGUAGES:
        return False
    if parts and len(parts[0]) == 4 and parts[0].isalpha():
        parts.pop(0)
    if parts:
        region = parts.pop(0)
        if not (region.upper() in _REGIONS or (len(region) == 3 and region.isdigit())):
            return False
    return not parts


def analyze_hreflang(result: CrawlResult) -> list[Issue]:
    pages = {page.url: page for page in result.pages}
    issues: list[Issue] = []
    for page in result.pages:
        if not page.hreflang:
            continue
        issues.extend(_page_issues(page, pages))
    return issues


def _page_issues(page: Page, pages: dict[str, Page]) -> list[Issue]:
    issues: list[Issue] = []
    by_language: dict[str, list[HreflangReference]] = {}
    for reference in page.hreflang:
        by_language.setdefault(reference.language.lower(), []).append(reference)
        if not valid_language_tag(reference.language):
            issues.append(_issue("hreflang.invalid_language", page.url, f"Invalid hreflang value: {reference.language}", {"language": reference.language, "target_url": reference.url}))
    for language, references in by_language.items():
        targets = sorted({reference.url for reference in references})
        if len(references) > 1:
            issues.append(_issue("hreflang.duplicate_language", page.url, f"Hreflang {language} is declared {len(references)} times", {"language": language, "targets": targets}))
    if page.url not in {reference.url for reference in page.hreflang}:
        issues.append(_issue("hreflang.missing_self", page.url, "Hreflang set does not reference the current page"))
    for reference in page.hreflang:
        target = pages.get(reference.url)
        if not target:
            continue
        if page.url not in {item.url for item in target.hreflang}:
            issues.append(_issue("hreflang.missing_return", page.url, f"{reference.url} does not reference this page", {"target_url": reference.url}))
        if target.status >= 400:
            issues.append(_issue("hreflang.target_http_error", page.url, f"Target returns HTTP {target.status}", {"target_url": reference.url, "status": target.status}))
        if target.redirect_hops:
            issues.append(_issue("hreflang.target_redirect", page.url, f"Target redirects to {target.final_url}", {"target_url": reference.url, "final_url": target.final_url}))
        if {"noindex", "none"} & set(target.robots_directives):
            issues.append(_issue("hreflang.target_noindex", page.url, "Target is noindex", {"target_url": reference.url}))
        if target.canonical_url and target.canonical_url != target.url:
            issues.append(_issue("hreflang.target_noncanonical", page.url, f"Target canonicalizes to {target.canonical_url}", {"target_url": reference.url, "canonical_url": target.canonical_url}))
    return issues


def _issue(rule_id: str, url: str, message: str, evidence: dict[str, object] | None = None) -> Issue:
    rule = get_rule(rule_id)
    return Issue(rule.id, rule.title, rule.severity, "page", url, message, evidence or {}, [], rule.remediation)
