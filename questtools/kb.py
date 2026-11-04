"""Knowledge base for Quest/Android packages.

A curated list of tested entries always wins; the name heuristic only
applies to packages that are not in the list.
"""

from __future__ import annotations

from dataclasses import dataclass

# Verdicts
PROTECTED = "protected"  # system refuses pm disable
KEEP = "keep"            # breaks core functions - only with --force
CAUTION = "caution"      # can be disabled, but with noticeable side effects
PRIVACY = "privacy"      # telemetry/ads/data collection - recommended
OPTIONAL = "optional"    # convenience feature, can be disabled if unused


@dataclass(frozen=True)
class Entry:
    verdict: str
    note: str


KB: dict[str, Entry] = {
    # ---- protected -------------------------------------------------------
    "com.oculus.updater": Entry(PROTECTED, "System updates. Cannot be disabled via adb (protected package)."),
    "com.oculus.q4bservice": Entry(PROTECTED, "Quest for Business service. Cannot be disabled via adb."),

    # ---- always keep -----------------------------------------------------
    "com.oculus.ocms": Entry(KEEP, "Content/app management. Without ocms there are no home panels, library "
                                   "or universal menu (VrShell: Placement request Timeout)."),
    "com.oculus.store": Entry(KEEP, "License sync. Without the store, purchased apps disappear from the "
                                    "library (\"not purchased\")."),
    "com.meta.credentialsmanager": Entry(KEEP, "Account/license sign-in, required for the store sync."),
    "com.oculus.vrshell": Entry(KEEP, "The VR shell itself."),
    "com.oculus.systemux": Entry(KEEP, "System UI (settings, bars)."),
    "com.oculus.horizon": Entry(KEEP, "Horizon platform service (account, store backend, platform API)."),
    "com.oculus.guardian": Entry(KEEP, "Guardian/boundary - safety feature."),
    "com.oculus.guardiansetup": Entry(KEEP, "Play area setup."),
    "com.oculus.guardianresources": Entry(KEEP, "Resources for Guardian."),
    "horizonos.platform": Entry(KEEP, "Horizon OS core platform."),
    "horizon.platform.service": Entry(KEEP, "Platform service for apps."),
    "horizon.platform.providers": Entry(KEEP, "Platform content providers."),
    "horizonos.openxr.runtimebroker": Entry(KEEP, "OpenXR runtime - VR apps won't run without it."),
    "com.android.systemui": Entry(KEEP, "Android system UI."),
    "com.android.settings": Entry(KEEP, "Android settings."),
    "com.android.providers.settings": Entry(KEEP, "System settings storage."),
    "com.android.shell": Entry(KEEP, "The adb shell itself."),
    "android": Entry(KEEP, "Android framework."),

    # ---- privacy: recommended (tested on v209 on 2026-10-27) -------------
    "com.oculus.bugreportuploader": Entry(PRIVACY, "Uploads bug reports to Meta."),
    "com.oculus.bugreportservice": Entry(PRIVACY, "Collects bug reports."),
    "com.oculus.ovrmonitormetricsservice": Entry(PRIVACY, "Performance/usage metrics to Meta."),
    "com.meta.federatedcomputing.oculus": Entry(PRIVACY, "Federated learning - model training on user data."),
    "com.meta.horizonadidservice": Entry(PRIVACY, "Advertising ID service."),
    "com.oculus.quickpromotionservice": Entry(PRIVACY, "Ads/promo pop-ups."),
    "com.oculus.systemintelligence": Entry(PRIVACY, "Usage analysis/suggestions."),
    "com.oculus.websafety": Entry(PRIVACY, "URL checks via Meta servers."),
    "com.oculus.backuptransportservice": Entry(PRIVACY, "Cloud backup to Meta. Off = no cloud backup."),
    "com.oculus.unifiedtelemetry": Entry(PRIVACY, "Central telemetry (older firmware)."),
    "com.oculus.os.logcollector": Entry(PRIVACY, "Log collector (older firmware)."),

    # ---- optional convenience features (also tested) ---------------------
    "com.oculus.assistant": Entry(OPTIONAL, "Voice assistant / voice commands."),
    "com.oculus.socialplatform": Entry(OPTIONAL, "Social features (friends, parties, invites)."),
    "com.oculus.magicislandcastingservice": Entry(OPTIONAL, "Casting to phone/TV."),
    "com.oculus.metacam": Entry(OPTIONAL, "Screenshots/videos from the menu. Off = no system captures "
                                          "(questtools shot without --meta still works)."),
    "com.oculus.remotedesktop": Entry(OPTIONAL, "Meta Remote Desktop (PC monitor in VR)."),
    "com.oculus.nux.ota": Entry(OPTIONAL, "Update step of the initial setup."),
    "com.oculus.q4b.mdm": Entry(OPTIONAL, "Device management for business customers."),

    # ---- caution ---------------------------------------------------------
    "com.oculus.companion.server": Entry(CAUTION, "Connection to the Meta Horizon phone app (pairing, casting)."),
    "com.oculus.firsttimenux": Entry(CAUTION, "Initial setup - needed after a factory reset."),
    "com.meta.environment.prod.nuxd": Entry(CAUTION, "Setup/environment service."),
    "com.oculus.notification_proxy": Entry(CAUTION, "Notifications (including phone forwarding)."),
    "com.oculus.appsafety": Entry(CAUTION, "App safety check; effect untested."),
    "com.oculus.gatekeeperservice": Entry(CAUTION, "Meta feature flags; disabling may lock features."),
    "com.facebook.horizon": Entry(CAUTION, "Horizon Worlds / social hub."),
    "com.oculus.horizonmediaplayer": Entry(CAUTION, "System media player."),
}

# Built-in profiles: packages that should be disabled.
BUILTIN_PROFILES: dict[str, dict] = {
    "telemetry": {
        "description": "Disable only telemetry, ads and data collection (minimal, safe).",
        "disable": sorted(p for p, e in KB.items() if e.verdict == PRIVACY),
    },
    "privacy": {
        "description": "Telemetry + unused Meta convenience services (tested on a Quest 3 with Horizon OS v209, 2026-10-27).",
        "disable": sorted(p for p, e in KB.items() if e.verdict in (PRIVACY, OPTIONAL)),
    },
}

VENDORS = ("com.meta.", "com.oculus.", "com.facebook.", "com.google.android.", "com.miui.", "com.xiaomi.",
           "com.samsung.android.", "com.huawei.", "com.oppo.", "com.oplus.", "com.vivo.", "com.amazon.")
TELEMETRY_WORDS = ("telemetry", "analytics", "metric", "diagnostic", "bugreport", "crashreport",
                   "logcollector", "datacollection", "experiment", "survey", "adid", "promotion")
CORE_PREFIXES = ("com.android.", "android.", "com.google.android.gms", "com.google.android.gsf")

CATEGORY_ORDER = ["TELEMETRY", "META", "SYSTEM", "USER", "OTHER"]


def lookup(pkg: str) -> Entry | None:
    return KB.get(pkg)


def is_telemetry(pkg: str) -> bool:
    e = KB.get(pkg)
    if e:
        return e.verdict == PRIVACY
    low = pkg.lower()
    return low.startswith(VENDORS) and any(w in low for w in TELEMETRY_WORDS) and not low.startswith(CORE_PREFIXES)


def classify(pkg: str, system: bool) -> tuple[str, str, str, str]:
    """-> (category, risk, verdict, note). Risk = consequences of DISABLING."""
    e = KB.get(pkg)
    low = pkg.lower()
    if e:
        risk = {PROTECTED: "HIGH", KEEP: "HIGH", CAUTION: "MEDIUM", PRIVACY: "LOW", OPTIONAL: "LOW"}[e.verdict]
        cat = "TELEMETRY" if e.verdict == PRIVACY else "SYSTEM" if low.startswith(("com.android.", "android")) \
            else "META"
        return cat, risk, e.verdict, e.note
    if is_telemetry(pkg):
        return "TELEMETRY", "LOW", PRIVACY, "Telemetry by name pattern (not verified)."
    if not system:
        return "USER", "LOW", OPTIONAL, "User-installed app."
    if low.startswith(CORE_PREFIXES) or low == "android":
        return "SYSTEM", "HIGH", KEEP, "Android core component."
    if low.startswith(("com.meta.", "com.oculus.", "com.facebook.", "horizon")):
        return "META", "MEDIUM", CAUTION, "Meta system service, not tested yet."
    return "OTHER", "MEDIUM", CAUTION, "Unknown system package."


VERDICT_LABEL = {
    PROTECTED: ("protected", "grey"),
    KEEP: ("keep", "red"),
    CAUTION: ("caution", "yellow"),
    PRIVACY: ("recommended", "green"),
    OPTIONAL: ("optional", "cyan"),
}
