from contextlib import redirect_stdout
from io import StringIO
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

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
        self.assertIn("first segment", result)

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

    def test_resume_keeps_existing_channels_and_checks_only_unfinished(self):
        existing = "#EXTM3U\n#EXTINF:-1,First\nhttps://one.example/live\n"
        source = (
            existing
            + "#EXTINF:-1,Second\nhttps://two.example/live\n"
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_found = root / "allFound.m3u"
            working = root / "working.m3u"
            all_found.write_text(existing, encoding="utf-8")
            working.write_text(existing, encoding="utf-8")
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
                    return_value=("https://raw.example/channels.m3u", source.encode()),
                ),
                patch.object(collector, "check_channel", return_value="responded") as check,
            ):
                collector.discover(["query"], all_found, 2, 5, 1, resume=True)
                count = collector.validate_all(all_found, working, 1, 0, resume=True)

            self.assertEqual(all_found.read_text(encoding="utf-8").count("#EXTINF:"), 2)
            self.assertEqual(working.read_text(encoding="utf-8").count("#EXTINF:"), 2)
            check.assert_called_once()
            self.assertIn("two.example", check.call_args.args[0])
            self.assertEqual(count, 2)

    def test_validation_reports_progress_percent_and_eta(self):
        self.assertEqual(collector.format_percent(2.9), "2,9")
        self.assertEqual(collector.format_percent(10), "10")
        self.assertEqual(collector.format_percent(11), "11")
        self.assertEqual(collector.format_percent(100), "100")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "allFound.m3u"
            working = root / "working.m3u"
            source.write_text(
                "#EXTM3U\n#EXTINF:-1,One\nhttps://one.example/live\n"
                "#EXTINF:-1,Two\nhttps://two.example/live\n",
                encoding="utf-8",
            )
            output = StringIO()
            with patch.object(collector, "check_channel", return_value="responded"), redirect_stdout(output):
                collector.validate_all(source, working, 1, 0)
            self.assertRegex(
                output.getvalue(),
                r"\[1/2 50% ETA: \d{2}:\d{2}:\d{2}\] \[WORKS\] One",
            )
            self.assertIn("[2/2 100% ETA: 00:00:00] [WORKS] Two", output.getvalue())

    def test_validation_is_parallel_and_respects_worker_limit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "allFound.m3u"
            working = root / "working.m3u"
            entries = "#EXTM3U\n" + "".join(
                f"#EXTINF:-1,Channel {index}\nhttps://channel{index}.example/live\n"
                for index in range(6)
            )
            source.write_text(entries, encoding="utf-8")
            lock = threading.Lock()
            active = 0
            maximum_active = 0

            def check_channel(*args):
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return "responded"

            with patch.object(collector, "check_channel", side_effect=check_channel), redirect_stdout(StringIO()):
                count = collector.validate_all(source, working, 1, 0, workers=3)

            self.assertEqual(count, 6)
            self.assertGreaterEqual(maximum_active, 2)
            self.assertLessEqual(maximum_active, 3)

    def test_search_queries_run_in_parallel_and_deduplicate_repositories(self):
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def fake_api(path, timeout, token):
            nonlocal active, maximum_active
            query = parse_qs(urlsplit(path).query)["q"][0]
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {"items": [
                {"full_name": "shared/repo"},
                {"full_name": f"{query}/repo"},
            ]}

        with patch.object(collector, "_api_json", side_effect=fake_api):
            repositories = list(
                collector.search_repositories(["first", "second"], 5, 1, workers=2)
            )
        self.assertGreaterEqual(maximum_active, 2)
        self.assertLessEqual(maximum_active, 2)
        self.assertEqual({repo["full_name"] for repo in repositories}, {
            "shared/repo", "first/repo", "second/repo",
        })

    def test_repository_trees_and_playlist_downloads_run_in_parallel(self):
        repositories = [
            {"full_name": f"owner{index}/repo", "default_branch": "main"}
            for index in range(3)
        ]
        lock = threading.Lock()
        active_trees = 0
        max_trees = 0
        active_downloads = 0
        max_downloads = 0

        def track_tree(repository, *args):
            nonlocal active_trees, max_trees
            with lock:
                active_trees += 1
                max_trees = max(max_trees, active_trees)
            time.sleep(0.03)
            with lock:
                active_trees -= 1
            return iter(["channels.m3u"])

        def track_download(repository, path, *args):
            nonlocal active_downloads, max_downloads
            with lock:
                active_downloads += 1
                max_downloads = max(max_downloads, active_downloads)
            time.sleep(0.03)
            with lock:
                active_downloads -= 1
            channel = repository["full_name"]
            content = f"#EXTM3U\n#EXTINF:-1,{channel}\nhttps://{channel}.example/live\n"
            return f"https://raw.example/{channel}.m3u", content.encode()

        with TemporaryDirectory() as directory:
            all_found = Path(directory) / "allFound.m3u"
            with (
                patch.object(collector, "search_repositories", return_value=iter(repositories)),
                patch.object(collector, "repository_playlists", side_effect=track_tree),
                patch.object(collector, "download_playlist", side_effect=track_download),
                redirect_stdout(StringIO()),
            ):
                count = collector.discover(["query"], all_found, 3, 10, 1, workers=3)

        self.assertEqual(count, 3)
        self.assertGreaterEqual(max_trees, 2)
        self.assertLessEqual(max_trees, 3)
        self.assertGreaterEqual(max_downloads, 2)
        self.assertLessEqual(max_downloads, 3)

    def test_restart_clears_previous_discovery(self):
        with TemporaryDirectory() as directory:
            all_found = Path(directory) / "allFound.m3u"
            all_found.write_text(
                "#EXTM3U\n#EXTINF:-1,Old\nhttps://old.example/live\n",
                encoding="utf-8",
            )
            with patch.object(collector, "search_repositories", return_value=iter(())):
                count = collector.discover(["query"], all_found, 1, 1, 1, resume=False)
            self.assertEqual(count, 0)
            self.assertEqual(all_found.read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_no_resume_clears_both_files_before_search(self):
        with TemporaryDirectory() as directory:
            all_found = Path(directory) / "allFound.m3u"
            working = Path(directory) / "working.m3u"
            old = "#EXTM3U\n#EXTINF:-1,Old\nhttps://old.example/live\n"
            all_found.write_text(old, encoding="utf-8")
            working.write_text(old, encoding="utf-8")
            args = [
                "createIPTFList.py",
                "--all-found", str(all_found),
                "--working", str(working),
                "--no-resume",
            ]
            with patch("sys.argv", args), patch.object(
                collector,
                "discover",
                side_effect=collector.GitHubAPIError("offline"),
            ):
                with self.assertRaises(SystemExit):
                    collector.main()
            self.assertEqual(all_found.read_text(encoding="utf-8"), "#EXTM3U\n")
            self.assertEqual(working.read_text(encoding="utf-8"), "#EXTM3U\n")


if __name__ == "__main__":
    unittest.main()
