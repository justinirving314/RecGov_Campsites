import pandas as pd
import requests
import numpy as np
from bs4 import BeautifulSoup
import json
from datetime import datetime, timedelta, date
import smtplib
import time
import certifi
import ssl
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from chromedriver_py import binary_path # this will get you the path variable


def make_conn_str(campsite, req_date):

    '''
    Function to make connection string based on campsite id and requested date
    '''

    conn_str = 'https://www.recreation.gov/api/camps/availability/campground/'+str(campsite)+'/month?start_date='+str(req_date)+'T00%3A00%3A00.000Z'

    return conn_str



def rec_api_pull(conn_str, headers):
    '''
    Function to pull data from recreation.gov API for the site and date requested
    The API can only return dates for the first of the month
    '''

    rec_gov_req = requests.get(conn_str,headers = headers, verify=False)
    rec_gov_content = rec_gov_req.content
    rec_gov_content = BeautifulSoup(rec_gov_content, "html.parser")
    rec_gov_content = json.loads(str(rec_gov_content))

    return rec_gov_content



def check_site(site, json_content):
    '''
    Function to parse JSON content returned from recreation.gov and convert to a dataframe
    '''


    location = []
    campsite_list = []
    date_list = []
    reserved_list = []

    # Cycle through all campsite at the given location
    for campsite in json_content['campsites'].keys():

        #Cycle through all dates for each campsite at the given location
        for key, item in json_content.get('campsites')[str(campsite)]['availabilities'].items():

            location.append(str(site))
            campsite_list.append(str(json_content['campsites'][str(campsite)]['site']))
            date_list.append(pd.to_datetime(str(key)).date())
            reserved_list.append(str(item))

    # Convert lists to Panadas Dataframe with results for each Location
    comb_list = zip(location, campsite_list, date_list, reserved_list)
    camp_df = pd.DataFrame(comb_list, columns=['Location','Campsite','Date','Availability'])

    return camp_df



def date_lookup(dates):

    '''
    Function to reduce the list of dates to only the first of the month for each date as API doesn't accept anything but
    first of the month
    '''

    date_list = []

    # Cycle through all dates in the list of dates input by the user
    for cur_date in dates:
        beg_month = (pd.to_datetime(cur_date)+timedelta(days=1))-pd.offsets.MonthBegin(1)
        date_list.append(beg_month)

    # Return a list of unique dates based on the first of month for each input date. Only want to query
    # the recreation.gov API with first of month dates.
    date_list = list(dict.fromkeys(date_list))

    return date_list


def execute_lookup(campsites,dates,headers):
    '''
    Function to execute availability lookup based on user inputs
    '''

    # Create an empty dataframe to store results
    all_df = pd.DataFrame()
    date_list = date_lookup(dates)

    # Cycle through all campsites in the user-input list of sites
    for site in campsites:

    # Cycle through all the dates for the given campsite based on the list of user-input dates
        for curr_date in date_list:

            curr_date = date_list[0].date()
            conn_str = make_conn_str(site, curr_date)
            rec_pull = rec_api_pull(conn_str, headers)
            site_info = check_site(site, rec_pull)

            # Combine results for the specific site with results for all sites
            all_df = pd.concat([all_df, site_info])

    # Filter results to only include those that were shown in the user-input dates list
    all_df = all_df[pd.to_datetime(all_df['Date']).isin(np.array(pd.to_datetime(dates)))]

    # Filter results to only include sites with availability
    all_df = all_df[all_df['Availability']=='Available']

    all_df.sort_values(['Location','Date'], inplace=True)

    return all_df



def send_email(all_df, email_add,email_pw,notification_emails):

    '''
    Script to write emails with available sites
    '''

    for not_email in notification_emails.split(','):
        gmail_user = email_add
        gmail_password = email_pw
        # rowitsfyzxwetxrc

        sent_from = gmail_user
        to = not_email
        subject = 'Campsites Available for Your Request!'
        body = all_df

        email_text = """\
        From: %s
        To: %s
        Subject: %s

        %s
        """ % (sent_from, to, subject, body)

        try:
            smtp_server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
            smtp_server.ehlo()
            smtp_server.login(gmail_user, gmail_password)
            smtp_server.sendmail(sent_from, to, email_text)
            smtp_server.close()
            print ("Campsites Found!")

        except Exception as ex:
            print ("Email Login Error….",ex)


def reserve_site(siteid, all_df, email_add, email_pw):

    # Create variables from all_df
    dates = all_df['Date'].unique()
    startdate = min(dates).strftime("%m/%d/%Y")
    enddate = (max(dates)+timedelta(days=1)).strftime("%m/%d/%Y")

    # Initialize Chrome Driver
    chrome_options = Options()

    # Open headless browser window for faster searching
    chrome_options.add_argument("--headless")

    # Ignore images on browser load
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")

    # Load Chrome Driver
    driver = webdriver.Chrome('./chromedriver',chrome_options=chrome_options)

    # Connect to campsite web page
    driver.get("https://www.recreation.gov/camping/campgrounds/"+str(siteid))

    # Set start/end dates and campsite id in filter fields to limit availability table
    # If this is not done the availability buttons may not be in the original page view
    start_date = driver.find_element(By.ID, "campground-start-date-calendar")
    end_date = driver.find_element(By.ID, "campground-end-date-calendar")
    start_date.send_keys(str(startdate))
    end_date.send_keys(str(enddate))

    # Ignore the site filter for now as it may not be necessary and slows down the process
    #site_filter = driver.find_element(By.ID, "campsite-filter-search")
    #site_filter.send_keys(str(campsite))

    # Cycle through all dates and the available campsites for that date to see if still available
    # on website. If a certain date gets booked successfully then move on to the next date
    # **Still need to consider how this works if the same site is available consecutive days as this changes how it looks in table (adds one day) ***
    for i in dates:

        campsites = all_df[all_df['Date']==i]['Campsite'].unique()

        for j in campsites:

            try:
                # Click on the availablity button based on the derived label string
                # Month is going to be three character descriptor
                # Day is going to be two character descriptor
                # Year is going to be four character descriptor
                # Site is going to be three character descriptor
                date_formatted = pd.to_datetime(i)
                month_name = date_formatted.strftime("%b")
                day_num = date_formatted.strftime("%d")
                year_num = date_formatted.strftime("%Y")

                button_str = "[aria-label='"+str(month_name)+" "+str(day_num)+", "+str(year_num)+" - Site "+ str(j)+" is available']"

                # Find and click button based on aria-label created in previous step
                avail_but = driver.find_element(By.CSS_SELECTOR, button_str)
                driver.execute_script("arguments[0].click();", avail_but)

                continue

            except Exception:
                pass

    # Click checkout button once availability buttons are selected
    check_out_but = driver.find_element(By.XPATH,("//*[@id='tabs-panel-0']/div[2]/div[3]/div/div[1]/div/div[2]/div/div/div[2]/div/button[2]"))
    driver.execute_script("arguments[0].click();", check_out_but)

    # Login using provided credentials
    email = driver.find_element(By.ID, "email")
    email.send_keys(email_add)
    pw = driver.find_element(By.ID, "rec-acct-sign-in-password")
    pw.send_keys(email_pw)

    # Press login button
    login_but = driver.find_element(By.XPATH,("/html/body/div[9]/div/div/div/div[2]/div/div/div[2]/form/button"))
    driver.execute_script("arguments[0].click();", login_but)

    # Wait until page has had a chance to load and then quit. Login with browser following
    # notification will allow reservation to be completed
    time.sleep(20)
    driver.quit()

def display_cur_cred():
    f = open("email_cred.json")
    email_cred = json.load(f)
    email_add = email_cred['email']
    email_pw = email_cred['password']
    notification_emails = email_cred['output email addresses']
    print('Current email address for sending notifications: ' + email_add)
    print('Current password for email address: ' + email_pw)
    print('Current list of notification email addresses: ' + notification_emails)
    return email_add,email_pw,notification_emails

def update_cur_cred(email_prev, email_pw_prev):
    print('Do you want to update sending email address and password (Y/N)?')
    email_q = input()

    if email_q.lower() =='y':
        print('Please enter the email address you would like to send emails from: ')
        email_add = input()
        print('Please enter the password associated with the email address: ')
        email_pw = input()
    else:
        email_add = email_prev
        email_pw = email_pw_prev

    print("Please enter a comma-separated list of emails addresses enclosed in single quotes (e.g. 'test@gmail.com') that should receive notifications when campsites are found: ")
    notification_emails = input()
    return email_add,email_pw,notification_emails

# Write JSON File
def write_email_cred(email_add,email_pw,notification_emails):
    email_cred = {
        "email": str(email_add),
        "password": str(email_pw),
        "output email addresses": notification_emails
    }

    # Serializing json
    json_object = json.dumps(email_cred, indent=4)

    # Writing to sample.json
    with open("email_cred.json", "w") as outfile:
        outfile.write(json_object)

def email_cred():
    try:
        f = open('email_cred.json')
        email_add,email_pw,notification_emails = display_cur_cred()
        print('Are these the credentials for email notification you would like to use (Y/N)?')
        cred_q = input()

        if cred_q.lower() == 'n':
            email_add,email_pw,notification_emails = update_cur_cred(email_add,email_pw)
            write_email_cred(email_add,email_pw,notification_emails)
            return email_add,email_pw,notification_emails

        else:
            return email_add,email_pw,notification_emails

    except IOError:
        print('No config file found, please enter credentials...')
        email_add,email_pw,notification_emails = update_cur_cred()
        write_email_cred(email_add,email_pw,notification_emails)
        return email_add,email_pw,notification_emails

def main_script(campsites, dates):

    # Define email credentials for sending notifications
    email_add,email_pw,notification_emails = email_cred()


    # Define headers for get request
    headers = { 'Accept-Language' : 'en-US',
            'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.9; rv:25.0) Gecko/20100101 Firefox/25.0'}
    all_df = pd.DataFrame()

    # Main while loop to check for campsites
    while len(all_df) == 0:

        try:

            # Execute main lookup and return a dataframe of all results
            all_df = execute_lookup(campsites,dates,headers)

            # Send email if there are results returned for available dates within your range and list of sites
            if len(all_df) != 0:
                send_email(all_df, email_add,email_pw,notification_emails)

                #Cycle through all sites to check -- maybe consider moving this?
                locations = all_df['Location'].unique()

                for i in locations:
                    reserve_site(i,all_df,email_add,email_pw)

            else:
                now = datetime.now()
                dt_string = now.strftime("%d/%m/%Y %H:%M:%S")
                print('No results at '+ dt_string)
            time.sleep(60)

        except KeyboardInterrupt:
            return

'''
Below is an example of how the functions are executed.
'''
# Create list of campsites
campsites = [232447]

# Create list of dates of interest
dates = ['2023-10-30']


main_script(campsites,dates)
