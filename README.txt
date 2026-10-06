KOPilot KO Integrated v1.6 — TEST branch
========================================
Stable baseline after promotion:
  ko-wip @ verified rebrand content (source ko-rebrand e4d48a534f53fb5c32ac477780d9846a856c3e3b)
Experimental branch:
  test

Branch policy:
- ko-wip     = verified/stable main branch
- test       = current experimental integrated branch
- ko-rebrand = keep temporarily as historical/rollback reference

Included in test:
1) YouTube Live real cabin microphone audio
   micd/rawAudioData 16 kHz mono PCM -> 44.1 kHz stereo AAC.
   Video continuity has priority; missing audio is silence-filled.

2) KO branding sweep
   - Remaining visible Korean '당근' -> KO/KOPilot wording
   - Remaining English/Chinese Carrot labels -> KO/KOPilot wording
   - KO Web fallback/update page
   - KO PILOT loading/build spinner
   - KO PILOT first-run intro (no old branded center image)
   Internal package/API/Param names remain where changing them could break compatibility.

3) Door-control candidate from the user's real capture
   Candidate: 0x3FF, bus 0, captured lock/unlock sequences with original inter-frame timing.
   INCLUDED BUT DISABLED BY DEFAULT.
   Panda safety is never bypassed.

PHONE / COMMA LOCAL TEST:
- Upload/extract package to e.g. /data/kopilot_v16
- Run:
    bash /data/kopilot_v16/APPLY_LOCAL_COMMA_v1.6.sh
- Local workflow pins ko-wip to the verified rebrand SHA and creates branch test.
- This local action does NOT move the GitHub ko-wip branch.

GitHub promotion:
- See PROMOTE_REBRAND_TO_KO_WIP.txt
- Promote ko-rebrand -> ko-wip once from your authenticated GitHub session.

Activate door candidate only after test boots:
    cd /data/openpilot
    bash scripts/kopilot_activate_door_candidate.sh
Then KO Web:
    Vehicle Aux -> KO 도어 제어 허용 = ON
Test only with ignition ON, P, standstill, CAN valid, and openpilot disengaged.
Attempt log:
    /data/ko/door_control.jsonl

Local rollback to stable:
    bash /path/to/ROLLBACK_LOCAL_TO_KO_WIP.sh

PC/GitHub push later:
    RUN_BUILD_AND_PUSH_v1.6.ps1
This requires remote ko-wip to already contain the verified rebrand commit.
Then fresh Comma install:
    INSTALL_COMMA4_TEST.ps1
