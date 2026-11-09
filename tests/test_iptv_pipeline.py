import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import createIPTFList as collector
import iptv_playlist
import runprogram_filtered as filtered


SAMPLE_PLAYLIST = (
    "#EXTM3U\n"
    "#EXTINF:-1 tvg-country=\"BR\",News Brasil\nhttps://good.example/live.m3u8\n"
    "#EXTINF:-1 tvg-country=\"US\",News US\nhttps://us.example/live.m3u8\n"
    "#EXTINF:-1 group-title=\"Erotic\",Miami TV\nhttps://adult.example/live.m3u8\n"
)


class PlaylistTests(unittest.TestCase):
    def test_adult_filter_preserves_adult_swim(self):
        content = (
            "#EXTM3U\n"
            "#EXTINF:-1 group-title=\"Erotic\",Miami TV\nhttps://adult.example/live.m3u8\n"
            "#EXTINF:-1 tvg-name=\"Adult Swim\",Adult Swim [4K]\nhttps://tv.example/live.m3u8\n"
        )
        result = iptv_playlist.filter_porn_entries(content).decode()
        self.assertNotIn("Miami TV", result)
        self.assertIn("Adult Swim [4K]", result)

    def test_hls_master_checks_first_segment(self):
        master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nmedia.m3u8\n"
        media = b"#EXTM3U\n#EXTINF:4,segment\nsegment.ts\n"
        with patch(
            "iptv_playlist.fetch_sample",
            side_effect=[
                ("https://host.test/master.m3u8", master),
                ("https://host.test/media.m3u8", media),
                ("https://host.test/segment.ts", b"segment bytes"),
            ],
        ):
            result = iptv_playlist.check_channel("https://host.test/master.m3u8")
        self.assertIn("primeiro segmento", result)

    def test_filters_by_term_and_country(self):
        rules = {
            "whitelist_terms": ["news"],
            "blacklist_terms": [],
            "whitelist_countries": ["br"],
            "blacklist_countries": [],
            "exclude_adult": True,
            "exclude_unmapped_non_latin": True,
            "group_script_channels": True,
            "group_unmapped_scripts": True,
            "language_character_groups": filtered.DEFAULT_RULES["language_character_groups"],
            "script_country_groups": filtered.DEFAULT_RULES["script_country_groups"],
        }
        result = filtered.filter_playlist(SAMPLE_PLAYLIST, rules).decode()
        self.assertIn("News Brasil", result)
        self.assertNotIn("News US", result)
        self.assertNotIn("Miami TV", result)

    def test_language_scripts_assign_country_groups(self):
        content = (
            "#EXTM3U\n"
            "#EXTINF:-1,中央电视台\nhttps://example.test/cn\n"
            "#EXTINF:-1,ニュース日本\nhttps://example.test/jp\n"
            "#EXTINF:-1,한국 뉴스\nhttps://example.test/kr\n"
            "#EXTINF:-1,Հայաստանի լուրեր\nhttps://example.test/am\n"
            "#EXTINF:-1,ქართული ამბები\nhttps://example.test/ge\n"
            "#EXTINF:-1,ຂ່າວລາວ\nhttps://example.test/la\n"
            "#EXTINF:-1,Новости\nhttps://example.test/ru\n"
            "#EXTINF:-1,قناة الأخبار\nhttps://example.test/ar\n"
            "#EXTINF:-1,समाचार\nhttps://example.test/hi\n"
        )
        rules = filtered.load_rules(filtered.DEFAULT_FILTERS)
        result = filtered.filter_playlist(content, rules).decode()
        self.assertIn('group-title="China"', result)
        self.assertIn('group-title="Japan"', result)
        self.assertIn('group-title="South Korea"', result)
        self.assertIn('group-title="Armenia"', result)
        self.assertIn('group-title="Georgia"', result)
        self.assertIn('group-title="Laos"', result)
        self.assertIn('group-title="Cyrillic"', result)
        self.assertIn('group-title="Arabic"', result)
        self.assertIn('group-title="Devanagari"', result)
        self.assertNotIn("Новости", filtered.filter_playlist(
            content,
            rules | {"exclude_unmapped_non_latin": True},
        ).decode())

    def test_language_characters_assign_presumed_groups(self):
        content = (
            "#EXTM3U\n"
            "#EXTINF:-1,Señal de España\nhttps://example.test/es\n"
            "#EXTINF:-1,¿Qué tal?\nhttps://example.test/es2\n"
            "#EXTINF:-1,Þáttur í dag\nhttps://example.test/is\n"
            "#EXTINF:-1,ðæö\nhttps://example.test/is2\n"
            "#EXTINF:-1,Nachrichten aus Berlin\nhttps://example.test/de\n"
        )
        rules = filtered.load_rules(filtered.DEFAULT_FILTERS)
        result = filtered.filter_playlist(content, rules).decode()
        self.assertEqual(result.count('group-title="spanish_presumed"'), 2)
        self.assertEqual(result.count('group-title="islandic_presumed"'), 2)
        self.assertIn("Nachrichten aus Berlin", result)
        self.assertNotIn("group-title=\"spanish_presumed\"", result.split("Nachrichten aus Berlin")[0].split("#EXTINF")[-1])

    def test_discovery_appends_and_validation_builds_working_list(self):
        clean_source = iptv_playlist.filter_porn_entries(SAMPLE_PLAYLIST.encode())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_found = root / "allFound.m3u"
            working = root / "working.m3u"
            with (
                patch.object(
                    collector,
                    "search_repositories",
                    return_value=iter([{"full_name": "owner/repo", "default_branch": "main"}]),
                ),
                patch.object(collector, "repository_playlists", return_value=iter(["channels.m3u"])),
                patch.object(
                    collector,
                    "download_playlist",
                    return_value=("https://raw.example/channels.m3u", clean_source),
                ),
                patch.object(collector, "check_channel", return_value="responded"),
            ):
                discovered = collector.discover(["query"], all_found, 2, 5, 1)
                working_count = collector.validate_all(all_found, working, 1, 0)

            self.assertEqual(discovered, 2)
            self.assertEqual(working_count, 2)
            self.assertIn("News Brasil", all_found.read_text(encoding="utf-8"))
            self.assertNotIn("Miami TV", all_found.read_text(encoding="utf-8"))
            self.assertEqual(working.read_text(encoding="utf-8").count("#EXTINF:"), 2)


if __name__ == "__main__":
    unittest.main()
