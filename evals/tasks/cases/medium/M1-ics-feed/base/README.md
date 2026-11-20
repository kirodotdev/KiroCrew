# ics-feed

`ics_feed.parse_ics` reads an iCalendar feed and returns the upcoming meetings
a dashboard lists, nearest first. A background poller also uses that list to
prepare a folder for each meeting shortly before it starts.

Run the tests with `python test_ics_feed.py`.
