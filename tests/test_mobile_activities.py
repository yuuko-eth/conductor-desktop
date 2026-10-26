"""list_windows(device_id=) on Android: a summary, never the raw dumpsys.

The raw `dumpsys activity activities` reply ran past the tool's size limit and
carried launcher metadata that included an SDK key (seen on
Android 14).
"""

from __future__ import annotations

from conductor_desktop.core.backends.mobile import _summarize_activities

# Trimmed from a real reply; the metadata line stands in for what leaked.
ANDROID_14 = """\
Display #0 (activities from top to bottom):
  * Task{ffc03f8 #1 type=home U=0 visible=true visibleRequested=false mode=fullscreen translucent=false sz=1}
      mLastPausedActivity: ActivityRecord{f4f3f52 u0 com.sec.android.app.launcher/.activities.LauncherActivity t562}
      * Hist  #0: ActivityRecord{f4f3f52 u0 com.sec.android.app.launcher/.activities.LauncherActivity t562}
        metaData=Bundle[{io.branch.sdk.BranchKey=key_live_SECRET}]
  * Task{50bd163 #568 type=standard A=1000:com.android.settings.root U=0 visible=false sz=2}
      * Hist  #0: ActivityRecord{9b41caf u0 com.android.settings/.SubSettings t568}
        resultTo=ActivityRecord{de22392 u0 com.android.settings/.Settings t568} resultWho=null
      * Hist  #0: ActivityRecord{de22392 u0 com.android.settings/.Settings t568}
  Resumed activities in task display areas (from top to bottom):
    Resumed: ActivityRecord{f4f3f52 u0 com.sec.android.app.launcher/.activities.LauncherActivity t562}
Display #1 (activities from top to bottom):
  ResumedActivity: ActivityRecord{f4f3f52 u0 com.sec.android.app.launcher/.activities.LauncherActivity t562}
"""

OLDER = """\
  Running activities (most recent first):
    TaskRecord{1a2b #12 A=com.example U=0 StackId=1 sz=1}
      Run #0: ActivityRecord{3c4d u0 com.example/.MainActivity t12}
  mResumedActivity: ActivityRecord{3c4d u0 com.example/.MainActivity t12}
"""


def test_android_14_summary() -> None:
    assert _summarize_activities(ANDROID_14) == {
        "resumed": "com.sec.android.app.launcher/.activities.LauncherActivity",
        "activities": [
            {"activity": "com.sec.android.app.launcher/.activities.LauncherActivity", "task": 562},
            {"activity": "com.android.settings/.SubSettings", "task": 568},
            {"activity": "com.android.settings/.Settings", "task": 568},
        ],
    }


def test_metadata_never_passes_through() -> None:
    assert "SECRET" not in str(_summarize_activities(ANDROID_14))


def test_older_android_run_lines() -> None:
    assert _summarize_activities(OLDER) == {
        "resumed": "com.example/.MainActivity",
        "activities": [{"activity": "com.example/.MainActivity", "task": 12}],
    }


def test_unparseable_dump_gives_empty_summary() -> None:
    assert _summarize_activities("garbage") == {"resumed": None, "activities": []}
