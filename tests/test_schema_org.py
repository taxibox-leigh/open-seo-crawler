"""Tests for semantic structured-data validation.

The risk this guards against is not missing findings, it is inventing them.
A validator that does not resolve @graph references or @id links reports every
Yoast page as broken, which is worse than reporting nothing.
"""
from __future__ import annotations

import unittest

from seo_scanner.analyzers.schema_org import validate_structured_data
from seo_scanner.analyzers.directives import extract_page_signals


def _blocks(*objects):
    return list(objects)


class SchemaValidationTest(unittest.TestCase):
    def test_missing_required_property_is_reported(self):
        validation = validate_structured_data(_blocks(
            {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "TAXIBOX"},
        ))
        missing = {(finding.node_type, finding.property_name) for finding in validation.missing_required}
        self.assertIn(("LocalBusiness", "address"), missing)
        self.assertNotIn(("LocalBusiness", "name"), missing)

    def test_recommended_and_required_are_separated(self):
        validation = validate_structured_data(_blocks(
            {"@context": "https://schema.org", "@type": "Article", "headline": "A headline"},
        ))
        self.assertEqual(validation.missing_required, [])
        recommended = {finding.property_name for finding in validation.missing_recommended}
        self.assertEqual(recommended, {"image", "datePublished", "author"})

    def test_graph_members_are_validated_individually(self):
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebSite", "@id": "#website", "name": "Site", "url": "https://example.com/"},
                {"@type": "Organization", "@id": "#org"},
            ],
        }))
        missing = {(finding.node_type, finding.property_name) for finding in validation.missing_required}
        self.assertEqual(missing, {("Organization", "name")})

    def test_id_references_resolve_across_the_graph(self):
        """A property pointing at another node counts as present.

        Yoast splits one entity across linked nodes; without resolution this
        page would report a missing address that is plainly there.
        """
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "LocalBusiness", "name": "TAXIBOX", "address": {"@id": "#address"}},
                {"@type": "PostalAddress", "@id": "#address",
                 "addressLocality": "Melbourne", "addressCountry": "AU",
                 "streetAddress": "1 Example St", "postalCode": "3000", "addressRegion": "VIC"},
            ],
        }))
        self.assertEqual(validation.missing_required, [])

    def test_unknown_types_are_skipped_not_flagged(self):
        validation = validate_structured_data(_blocks(
            {"@context": "https://schema.org", "@type": "SomeTypeWeDoNotModel", "whatever": 1},
        ))
        self.assertEqual(validation.missing_required, [])
        self.assertEqual(validation.missing_recommended, [])

    def test_local_business_subtypes_inherit_requirements(self):
        validation = validate_structured_data(_blocks(
            {"@context": "https://schema.org", "@type": "SelfStorage", "name": "TAXIBOX"},
        ))
        self.assertEqual(
            {finding.property_name for finding in validation.missing_required}, {"address"})

    def test_invalid_property_values_are_reported(self):
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org", "@type": "Article",
            "headline": "A headline", "author": "Someone",
            "image": "not-a-url", "datePublished": "last Tuesday",
        }))
        problems = {finding.property_name for finding in validation.invalid_values}
        self.assertEqual(problems, {"image", "datePublished"})

    def test_valid_values_produce_nothing(self):
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org", "@type": "Article",
            "headline": "A headline", "author": "Someone",
            "image": "https://example.com/image.jpg", "datePublished": "2026-08-12",
        }))
        self.assertEqual(validation.invalid_values, [])
        self.assertEqual(validation.missing_required, [])

    def test_empty_string_counts_as_missing(self):
        validation = validate_structured_data(_blocks(
            {"@context": "https://schema.org", "@type": "Organization", "name": "   "},
        ))
        self.assertEqual(
            {finding.property_name for finding in validation.missing_required}, {"name"})

    def test_type_arrays_are_all_validated(self):
        validation = validate_structured_data(_blocks(
            {"@context": "https://schema.org", "@type": ["Organization", "LocalBusiness"], "name": "TAXIBOX"},
        ))
        self.assertEqual(
            {finding.property_name for finding in validation.missing_required}, {"address"})

    def test_nested_offers_are_validated(self):
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org", "@type": "Product", "name": "Box",
            "offers": {"@type": "Offer", "price": "not a number"},
        }))
        missing = {(finding.node_type, finding.property_name) for finding in validation.missing_required}
        self.assertIn(("Offer", "priceCurrency"), missing)
        self.assertIn("price", {finding.property_name for finding in validation.invalid_values})

    def test_either_rating_count_satisfies_the_requirement(self):
        """Google accepts reviewCount or ratingCount; asking for both flags
        correct markup, which is exactly what the live check caught."""
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org", "@type": "AggregateRating",
            "ratingValue": 4.9, "reviewCount": 10553,
        }))
        self.assertEqual(validation.missing_required, [])
        self.assertEqual(validation.missing_recommended, [])

    def test_one_of_group_reports_when_all_members_are_absent(self):
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org", "@type": "AggregateRating", "ratingValue": 4.9,
        }))
        self.assertEqual(
            [finding.property_name for finding in validation.missing_recommended],
            ["reviewCount or ratingCount"])

    def test_final_breadcrumb_without_item_is_accepted(self):
        validation = validate_structured_data(_blocks({
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://example.com/"},
                {"@type": "ListItem", "position": 2, "name": "Current page"},
            ],
        }))
        self.assertEqual(validation.missing_required, [])
        self.assertEqual(validation.missing_recommended, [])

    def test_signals_expose_validation_to_the_scanner(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"LocalBusiness","name":"TAXIBOX"}'
            "</script></head><body></body></html>"
        )
        signals = extract_page_signals("https://example.com/", html)
        self.assertTrue(signals.schema_missing_required)
        self.assertEqual(signals.schema_missing_required[0]["property"], "address")
        self.assertEqual(signals.schema_missing_required[0]["type"], "LocalBusiness")


if __name__ == "__main__":
    unittest.main()
