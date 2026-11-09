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
        }
        result = filtered.filter_playlist(SAMPLE_PLAYLIST, rules).decode()
        self.assertIn("News Brasil", result)
        self.assertNotIn("News US", result)
        self.assertNotIn("Miami TV", result)

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
