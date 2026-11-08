"""Tests for scripts/publish_build.py against local bare repos standing in for GitHub.

    python3 -m unittest discover -s tests      (pytest works too)
"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import publish_build  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "publish_build.py")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8
APPS = {"alpha": "com.pbordjadze.alpha", "beta": "com.pbordjadze.beta", "gamma": "com.pbordjadze.gamma"}


def run(cwd, *cmd, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        raise AssertionError(f"{cmd}: {r.stderr}")
    return r


def make_ipa(path, bundle, build):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/App.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": bundle, "CFBundleVersion": str(build)}))
        z.writestr("Payload/App.app/App", "binary " + str(build))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.remote = os.path.join(self.dir, "remote.git")
        run(self.dir, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        seed = os.path.join(self.dir, "seed")
        os.makedirs(os.path.join(seed, "apps"))
        os.makedirs(os.path.join(seed, "icons"))
        config = {"name": "n", "identifier": "i", "subtitle": "s", "website": "w", "tintColor": "#000", "iconURL": "u",
                  "apps": [{"local": f"apps/{a}.json", "bundleIdentifiers": [b]} for a, b in APPS.items() if a != "gamma"]}
        json.dump(config, open(os.path.join(seed, "sources.json"), "w"))
        run(self.dir, "git", "init", "-q", "-b", "main", seed)
        run(seed, "git", "add", "-A")
        run(seed, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
        run(seed, "git", "push", "-q", self.remote, "main")

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, app):
        path = os.path.join(self.dir, f"{app}.manifest.json")
        json.dump({"name": app.title(), "bundleIdentifier": APPS[app], "subtitle": "sub", "localizedDescription": "desc"}, open(path, "w"))
        return path

    def icon(self):
        path = os.path.join(self.dir, "icon.png")
        open(path, "wb").write(PNG)
        return path

    def args(self, app, build, **extra):
        ipa = make_ipa(os.path.join(self.dir, f"{app}-{build}-{threading.get_ident()}.ipa"), extra.pop("bundle", APPS[app]), extra.pop("ipa_build", build))
        out = ["--app-id", app, "--version", f"1.0.{build}", "--build", str(build), "--ipa", ipa,
               "--manifest", self.manifest(app), "--icon", self.icon(), "--remote", self.remote]
        for k, v in extra.items():
            out += [f"--{k}", v]
        return out

    def publish(self, *a, check=True):
        r = subprocess.run([sys.executable, SCRIPT, *a], capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError(r.stderr)
        return r

    def show(self, ref, path):
        r = run(self.remote, "git", "show", f"{ref}:{path}", check=False)
        return r.stdout if r.returncode == 0 else None

    def builds_files(self):
        r = run(self.remote, "git", "ls-tree", "-r", "--name-only", "builds", check=False)
        return set(r.stdout.split())


class Publish(Base):
    def test_first_publish_and_keep_five(self):
        for build in range(1, 8):
            self.publish(*self.args("alpha", build, notes=f"note {build}"))
        entry = json.loads(self.show("main", "apps/alpha.json"))
        self.assertEqual([v["buildVersion"] for v in entry["versions"]], ["7", "6", "5", "4", "3"])
        self.assertEqual(entry["versions"][0]["downloadURL"],
                         "https://raw.githubusercontent.com/pbordjadze/sidestore/builds/alpha/Alpha-7.ipa")
        self.assertEqual(self.builds_files(), {f"alpha/Alpha-{b}.ipa" for b in range(3, 8)})
        self.assertEqual(run(self.remote, "git", "rev-list", "--count", "builds").stdout.strip(), "1")
        self.assertIsNotNone(self.show("main", "icons/alpha.png"))
        # the entry passes the real builder's checks
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build_source
        spec = {"local": "apps/alpha.json", "bundleIdentifiers": [APPS["alpha"]]}
        build_source.check_entry(spec, [entry])

    def test_republish_same_build_is_idempotent(self):
        a = self.args("alpha", 1, date="2026-11-08T10:00:00Z")
        self.publish(*a)
        before = run(self.remote, "git", "rev-parse", "main").stdout
        self.publish(*a)
        self.assertEqual(before, run(self.remote, "git", "rev-parse", "main").stdout)

    def test_other_apps_untouched(self):
        self.publish(*self.args("alpha", 1))
        alpha = self.show("main", "apps/alpha.json")
        self.publish(*self.args("beta", 4))
        self.assertEqual(alpha, self.show("main", "apps/alpha.json"))
        self.assertEqual(self.builds_files(), {"alpha/Alpha-1.ipa", "beta/Beta-4.ipa"})

    def test_later_publish_needs_no_manifest_or_icon(self):
        self.publish(*self.args("alpha", 1))
        a = self.args("alpha", 2)
        i = a.index("--manifest")
        del a[i:i + 2]
        i = a.index("--icon")
        del a[i:i + 2]
        self.publish(*a)
        entry = json.loads(self.show("main", "apps/alpha.json"))
        self.assertEqual(len(entry["versions"]), 2)
        self.assertEqual(entry["name"], "Alpha")


class Refusals(Base):
    def refused(self, args, text):
        r = self.publish(*args, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(text, r.stderr)
        self.assertEqual(run(self.remote, "git", "rev-list", "--count", "main").stdout.strip(), "1")
        self.assertIsNone(run(self.remote, "git", "rev-parse", "--verify", "-q", "builds", check=False).stdout.strip() or None)

    def test_app_not_in_sources_json(self):
        self.refused(self.args("gamma", 1), "sources.json has no")

    def test_bad_app_id(self):
        for bad in ("../x", "Alpha", "a/b", ""):
            a = self.args("alpha", 1)
            a[a.index("--app-id") + 1] = bad
            self.refused(a, "app id")

    def test_ipa_bundle_mismatch(self):
        self.refused(self.args("alpha", 1, bundle="com.evil.app"), "CFBundleIdentifier")

    def test_ipa_build_mismatch(self):
        self.refused(self.args("alpha", 2, ipa_build=3), "CFBundleVersion")

    def test_manifest_bundle_not_allowed(self):
        a = self.args("alpha", 1)
        path = a[a.index("--manifest") + 1]
        json.dump({"name": "Alpha", "bundleIdentifier": "com.evil.app"}, open(path, "w"))
        a[a.index("--ipa") + 1] = make_ipa(os.path.join(self.dir, "e.ipa"), "com.evil.app", 1)
        self.refused(a, "bundleIdentifiers")

    def test_manifest_cannot_carry_urls(self):
        a = self.args("alpha", 1)
        path = a[a.index("--manifest") + 1]
        json.dump({"name": "Alpha", "bundleIdentifier": APPS["alpha"], "downloadURL": "https://evil.example/a.ipa"}, open(path, "w"))
        self.refused(a, "not allowed")

    def test_icon_must_be_png(self):
        a = self.args("alpha", 1)
        open(a[a.index("--icon") + 1], "wb").write(b"nope")
        self.refused(a, "PNG")

    def test_first_publish_needs_icon(self):
        a = self.args("alpha", 1)
        i = a.index("--icon")
        del a[i:i + 2]
        self.refused(a, "--icon")

    def test_old_build_not_added(self):
        for b in range(10, 15):
            self.publish(*self.args("alpha", b))
        self.assertIn("older", self.publish(*self.args("alpha", 2), check=False).stderr)


class Races(Base):
    def test_competing_push_between_fetch_and_push(self):
        """Another publisher lands on both branches after we fetched: we must retry, not clobber."""
        self.publish(*self.args("alpha", 1))
        fired = []

        def competitor():
            if fired:
                return
            fired.append(1)
            subprocess.run([sys.executable, SCRIPT, *self.args("beta", 9)], check=True, capture_output=True)

        publish_build._after_fetch = competitor
        try:
            sys.argv = ["publish_build.py", *self.args("alpha", 2)]
            publish_build.main()
        finally:
            publish_build._after_fetch = lambda: None
        self.assertTrue(fired)
        self.assertEqual(self.builds_files(), {"alpha/Alpha-1.ipa", "alpha/Alpha-2.ipa", "beta/Beta-9.ipa"})
        self.assertEqual(len(json.loads(self.show("main", "apps/alpha.json"))["versions"]), 2)
        self.assertEqual(json.loads(self.show("main", "apps/beta.json"))["versions"][0]["buildVersion"], "9")

    def test_concurrent_publishers(self):
        jobs = [("alpha", b) for b in (1, 2, 3)] + [("beta", b) for b in (1, 2)]
        argvs = [self.args(a, b) for a, b in jobs]
        results = []

        def go(argv):
            results.append(subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True, text=True))

        threads = [threading.Thread(target=go, args=(a,)) for a in argvs]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual([r.stderr for r in results if r.returncode], [])
        for app, builds in (("alpha", {"1", "2", "3"}), ("beta", {"1", "2"})):
            entry = json.loads(self.show("main", f"apps/{app}.json"))
            self.assertEqual({v["buildVersion"] for v in entry["versions"]}, builds)
        self.assertEqual(self.builds_files(), {f"{a}/{a.title()}-{b}.ipa" for a, b in jobs})
        # main was never rewritten: a straight line of 1 seed + 5 publishes
        self.assertEqual(run(self.remote, "git", "rev-list", "--count", "--merges", "main").stdout.strip(), "0")
        self.assertEqual(run(self.remote, "git", "rev-list", "--count", "main").stdout.strip(), "6")


if __name__ == "__main__":
    unittest.main()
