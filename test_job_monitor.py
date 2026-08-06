import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_monitor import Job, Monitor, _discord_payloads, _jsonld_jobs


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = {
            "filters": {
                "include_any": [],
                "pm_signal_any": ["product manager", "product management", "associate product", "apm", "rpm"],
                "early_career_signal_any": ["new grad", "university grad", "early career", "entry level", "0-2 years"],
                "exclude_any": ["senior", "staff", "director", "product marketing", "program manager"],
                "locations_any": []
            },
            "companies": [{"name": "Official", "provider": "generic", "url": "https://example.com/jobs"}],
            "discovery_sources": [{"name": "Aggregator", "provider": "generic", "url": "https://example.com/search"}]
        }
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(config))
        self.monitor = Monitor(path, Path(self.tmp.name) / "db.sqlite")

    def tearDown(self):
        self.monitor.close()
        self.tmp.cleanup()

    def test_apm_matches(self):
        self.assertTrue(self.monitor.matches(Job("X", "1", "Associate Product Manager (APM)", "NY", "u")))

    def test_generic_pm_needs_early_career_signal(self):
        self.assertFalse(self.monitor.matches(Job("X", "1", "Product Manager", "NY", "u")))
        self.assertTrue(self.monitor.matches(Job("X", "2", "Product Manager, University Grad", "NY", "u")))

    def test_excludes_senior(self):
        self.assertFalse(self.monitor.matches(Job("X", "1", "Senior APM", "NY", "u")))

    def test_pm_mentioned_only_in_description_does_not_match(self):
        job = Job("X", "1", "Software Engineer, New Grad", "NY", "u", "Partner with product managers")
        self.assertFalse(self.monitor.matches(job))

    def test_apm_application_monitoring_is_not_product(self):
        job = Job("X", "1", "Engineering Manager - APM Serverless", "NY", "u", "Early career team")
        self.assertFalse(self.monitor.matches(job))

    def test_jsonld_graph(self):
        value = {"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "APM"}]}
        self.assertEqual(_jsonld_jobs(value)[0]["title"], "APM")

    def test_discord_batches_ten_embeds(self):
        jobs = [Job("X", str(i), f"APM {i}", "NY", f"https://x.test/{i}") for i in range(11)]
        payloads = _discord_payloads(jobs)
        self.assertEqual([len(p["embeds"]) for p in payloads], [10, 1])
        self.assertEqual(payloads[0]["allowed_mentions"], {"parse": []})
        self.assertEqual(payloads[0]["embeds"][0]["fields"][0]["value"], "X")

    def test_discovery_sources_are_opt_in(self):
        self.assertEqual([source["name"] for source in self.monitor.sources()], ["Official"])
        with patch.dict("os.environ", {"ENABLE_DISCOVERY_SOURCES": "true"}):
            self.assertEqual(
                [source["name"] for source in self.monitor.sources()],
                ["Official", "Aggregator"],
            )


if __name__ == "__main__":
    unittest.main()
