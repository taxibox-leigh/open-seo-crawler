"""Semantic validation of JSON-LD structured data.

The existing structured-data checks are syntactic: valid JSON, no duplicate
blocks, sane keyword shapes, a declared @context, resolvable local @id
references. None of them look at whether the markup actually says what the
type requires, so a site can carry hundreds of rich-result errors and report
a clean bill of health.

This module validates the properties of the types that earn rich results,
against a vendored requirement table. Two deliberate limits keep it honest:

* Only known types are validated. An unrecognised @type is skipped rather
  than reported — the table is a curated subset of schema.org, not a mirror
  of it, so "unknown" would mean "not in our table" far more often than it
  would mean "wrong".
* @graph members are validated individually and @id references are resolved
  across the whole document first. Yoast and similar plugins split one entity
  across several linked nodes; without resolution every reference would look
  like a missing property.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Per type: properties Google requires for rich-result eligibility, and
# properties it recommends. Sourced from Google's structured-data
# documentation. Keep additions conservative — a wrong entry here becomes a
# false positive on every page carrying that type.
REQUIREMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "Article": {"required": ("headline",), "recommended": ("image", "datePublished", "author")},
    "NewsArticle": {"required": ("headline",), "recommended": ("image", "datePublished", "author")},
    "BlogPosting": {"required": ("headline",), "recommended": ("image", "datePublished", "author")},
    "BreadcrumbList": {"required": ("itemListElement",), "recommended": ()},
    "FAQPage": {"required": ("mainEntity",), "recommended": ()},
    "Question": {"required": ("name", "acceptedAnswer"), "recommended": ()},
    "Answer": {"required": ("text",), "recommended": ()},
    "HowTo": {"required": ("name", "step"), "recommended": ("image", "totalTime")},
    "Recipe": {"required": ("name", "recipeIngredient", "recipeInstructions"), "recommended": ("image", "author")},
    "Event": {"required": ("name", "startDate", "location"), "recommended": ("image", "endDate", "offers")},
    "Product": {"required": ("name",), "recommended": ("image", "description", "brand"), "one_of": (("offers", "review", "aggregateRating"),)},
    "Offer": {"required": ("price", "priceCurrency"), "recommended": ("availability", "url")},
    # Google accepts either count; requiring both would flag correct markup.
    "AggregateRating": {"required": ("ratingValue",), "recommended": (), "one_of": (("reviewCount", "ratingCount"),)},
    "Review": {"required": ("reviewRating",), "recommended": ("author", "datePublished")},
    "Rating": {"required": ("ratingValue",), "recommended": ("bestRating", "worstRating")},
    "LocalBusiness": {"required": ("name", "address"), "recommended": ("telephone", "openingHours", "image", "url", "priceRange")},
    "Organization": {"required": ("name",), "recommended": ("url", "logo")},
    "WebSite": {"required": ("name", "url"), "recommended": ()},
    "WebPage": {"required": (), "recommended": ("name",)},
    "VideoObject": {"required": ("name", "thumbnailUrl", "uploadDate"), "recommended": ("description", "duration")},
    "JobPosting": {"required": ("title", "description", "datePosted", "hiringOrganization", "jobLocation"), "recommended": ("baseSalary", "employmentType")},
    "PostalAddress": {"required": ("addressLocality", "addressCountry"), "recommended": ("streetAddress", "postalCode", "addressRegion")},
    "ImageObject": {"required": ("url",), "recommended": ("width", "height")},
    "Service": {"required": ("name",), "recommended": ("provider", "areaServed", "serviceType")},
    # The final crumb in a BreadcrumbList legitimately omits item, so asking
    # for it would flag correct markup on every page that has breadcrumbs.
    "ListItem": {"required": ("position",), "recommended": ("name",)},
}

# Types that inherit LocalBusiness requirements. schema.org has many; these
# are the ones common enough to be worth naming.
LOCAL_BUSINESS_SUBTYPES = (
    "Store", "Restaurant", "ProfessionalService", "HomeAndConstructionBusiness",
    "MovingCompany", "SelfStorage", "AutomotiveBusiness", "FinancialService",
    "MedicalBusiness", "LodgingBusiness", "EntertainmentBusiness", "SportsActivityLocation",
)
for _subtype in LOCAL_BUSINESS_SUBTYPES:
    REQUIREMENTS.setdefault(_subtype, REQUIREMENTS["LocalBusiness"])

# Properties whose values must look like a URL, a date, or a number.
URL_PROPERTIES = frozenset({"url", "logo", "thumbnailUrl", "contentUrl", "sameAs", "image"})
DATE_PROPERTIES = frozenset({"datePublished", "dateModified", "datePosted", "startDate", "endDate", "uploadDate", "validFrom"})
NUMERIC_PROPERTIES = frozenset({"price", "ratingValue", "reviewCount", "ratingCount", "bestRating", "worstRating", "position", "width", "height"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass
class SchemaFinding:
    node_type: str
    property_name: str
    message: str
    node_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"type": self.node_type, "property": self.property_name, "message": self.message, "node_id": self.node_id}


@dataclass
class SchemaValidation:
    missing_required: list[SchemaFinding] = field(default_factory=list)
    missing_recommended: list[SchemaFinding] = field(default_factory=list)
    invalid_values: list[SchemaFinding] = field(default_factory=list)

    def as_dicts(self) -> tuple[list[dict], list[dict], list[dict]]:
        return (
            [finding.as_dict() for finding in self.missing_required],
            [finding.as_dict() for finding in self.missing_recommended],
            [finding.as_dict() for finding in self.invalid_values],
        )


def _types_of(node: dict) -> list[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def _collect_nodes(value: object, into: list[dict]) -> None:
    """Every typed object in the document, including @graph members."""
    if isinstance(value, dict):
        if _types_of(value):
            into.append(value)
        for key, child in value.items():
            if key.startswith("@") and key != "@graph":
                continue
            _collect_nodes(child, into)
    elif isinstance(value, list):
        for item in value:
            _collect_nodes(item, into)


def _index_by_id(nodes: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for node in nodes:
        identifier = node.get("@id")
        if isinstance(identifier, str) and identifier:
            index.setdefault(identifier, node)
    return index


def _resolve(value: object, by_id: dict[str, dict]) -> object:
    """Follow a bare {"@id": ...} reference to the node it names."""
    if isinstance(value, dict) and set(value) == {"@id"} and isinstance(value["@id"], str):
        return by_id.get(value["@id"], value)
    return value


def _is_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _value_problem(property_name: str, value: object) -> str:
    """Human-readable problem with a property value, or '' when it is fine."""
    if isinstance(value, list):
        for item in value:
            problem = _value_problem(property_name, item)
            if problem:
                return problem
        return ""
    if isinstance(value, dict):
        return ""  # nested nodes are validated in their own right
    if property_name in URL_PROPERTIES and isinstance(value, str):
        if not re.match(r"^(https?:)?//|^/", value.strip()):
            return f"{property_name} is not a URL: {value[:80]!r}"
    if property_name in DATE_PROPERTIES and isinstance(value, str):
        if not _DATE_RE.match(value.strip()):
            return f"{property_name} is not an ISO 8601 date: {value[:80]!r}"
    if property_name in NUMERIC_PROPERTIES:
        if isinstance(value, bool) or (isinstance(value, str) and not _NUMBER_RE.match(value.strip())):
            return f"{property_name} is not a number: {value if isinstance(value, bool) else repr(value[:80])}"
    return ""


def validate_structured_data(parsed_blocks: list[object]) -> SchemaValidation:
    """Validate every typed node across the page's JSON-LD blocks."""
    validation = SchemaValidation()
    nodes: list[dict] = []
    for block in parsed_blocks:
        _collect_nodes(block, nodes)
    by_id = _index_by_id(nodes)

    for node in nodes:
        node_id = node.get("@id") if isinstance(node.get("@id"), str) else ""
        for type_name in _types_of(node):
            rules = REQUIREMENTS.get(type_name)
            if rules is None:
                continue  # unknown to the table; not our place to judge
            for property_name in rules["required"]:
                if not _is_present(_resolve(node.get(property_name), by_id)):
                    validation.missing_required.append(SchemaFinding(
                        type_name, property_name,
                        f"{type_name} is missing the required property {property_name}", node_id or ""))
            for property_name in rules["recommended"]:
                if not _is_present(_resolve(node.get(property_name), by_id)):
                    validation.missing_recommended.append(SchemaFinding(
                        type_name, property_name,
                        f"{type_name} omits the recommended property {property_name}", node_id or ""))
            # Groups where any one member satisfies the requirement.
            for group in rules.get("one_of", ()):
                if not any(_is_present(_resolve(node.get(name), by_id)) for name in group):
                    validation.missing_recommended.append(SchemaFinding(
                        type_name, " or ".join(group),
                        f"{type_name} declares none of {', '.join(group)}", node_id or ""))
        for property_name, value in node.items():
            if property_name.startswith("@"):
                continue
            problem = _value_problem(property_name, _resolve(value, by_id))
            if problem:
                validation.invalid_values.append(SchemaFinding(
                    (_types_of(node) or ["Thing"])[0], property_name, problem, node_id or ""))
    return validation
