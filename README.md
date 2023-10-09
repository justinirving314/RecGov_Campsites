# Recreation.gov Campsite Checker
This is a Python script that automates checking if a campsite hosted on Recreation.gov is available for a given date, and then reserves the campsite using Selenium once an open campsite is found. The user is notified via email once a campsite is reserved and has about 10 minutes to complete booking of the campsite.

# Dependencies
- Python
- Selenium
- JSON
- Firefox

# Running the Script
Copy the main script file `campsite_checker.py` and modify the fields calling out the list of campsites and desired dates. The user will also need to modify the email credentials used for notification. The email address must be set-up using `smtp` as a regular GMAIL account will not work.
