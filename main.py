import os
import time
import threading
from datetime import datetime, timedelta, timezone
import socket
import struct
from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from antares_http import antares # Keep for Antares API
from zoneinfo import ZoneInfo

# --- Configuration ---
ANTARES_ACCESS_KEY = 'fe5c7a15d8c13220:bfd764392a99a094' # Your Antares Key
ANTARES_PROJECT_NAME = 'TADKT-1' # Your Antares Project
ANTARES_DEVICE_NAME = 'PMM' # Your Antares Device
DATABASE_FILE = 'mrp.db' # Renamed DB file
CHECK_INTERVAL_SECONDS = 2 # How often to RETRY within the fetch window
NTP_SERVER = 'pool.ntp.org' # For reliable time checks

app = Flask(__name__)
# Get the database URL from the environment variable provided by Railway
try:
    database_url = os.getenv('DATABASE_URL')
except:
    print("waiting for database url")
    time.sleep(1)
    
# A small but important fix: SQLAlchemy expects 'postgresql://' but Railway provides 'postgres://'
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# Use the Railway database URL if it exists, otherwise fall back to the local SQLite file
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or f'sqlite:///{DATABASE_FILE}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
app.jinja_env.globals['now'] = datetime.utcnow # For footer timestamp

# --- Database Model (Unchanged) ---
class dbReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    DateTime = db.Column(db.String(19), nullable=False, unique=True, index=True)
    Energy = db.Column(db.Float) 
    Power = db.Column(db.Float, nullable=True)
    Ampere = db.Column(db.Float, nullable=True)
    Voltage = db.Column(db.Float, nullable=True)
    Co2 = db.Column(db.Float, nullable=True)
    Co2_cost = db.Column(db.Float, nullable=True)

# --- Data Fetching Logic (Modified) ---
def get_ntp_time(server="pool.ntp.org"):
    """Gets current UTC time from an NTP server."""
    NTP_PORT, NTP_PACKET_FORMAT, NTP_DELTA = 123, "!12I", 2208988800
    client = None
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        data = b'\x1b' + 47 * b'\0'
        client.sendto(data, (server, NTP_PORT))
        data, _ = client.recvfrom(1024)
        if data:
            secs = struct.unpack(NTP_PACKET_FORMAT, data)[10]
            timestamp = secs - NTP_DELTA
            return datetime.fromtimestamp(timestamp, timezone.utc)
        return None
    except socket.timeout:
        print("NTP Error: Request timed out")
        return None
    except Exception as e:
        print(f"NTP Error: {e}")
        return None
    finally:
        if client: client.close()

def fetch_and_store_hourly_data(target_hour_dt_utc: datetime) -> bool:
    """
    Fetches data from Antares and stores it.
    Returns True on success, False on failure.
    """
    with app.app_context():
        formatted_ts = target_hour_dt_utc.strftime('%Y-%m-%d %H:%M:%S')

        # This check is a safeguard, but the main loop logic should prevent duplicates.
        if dbReading.query.filter_by(DateTime=formatted_ts).first():
            print(f"Store Warning: Data for {formatted_ts} UTC already exists. Skipping.")
            return False

        try:
            antares.setAccessKey(ANTARES_ACCESS_KEY)
            latest_data = antares.get(ANTARES_PROJECT_NAME, ANTARES_DEVICE_NAME)

            if not latest_data or 'content' not in latest_data:
                print(f"Store Error: Invalid or empty data received from Antares near {formatted_ts} UTC.")
                return False

            content = latest_data['content']
            
            # Basic check to ensure we have some expected data
            if content.get('Power') is None:
                print(f"Store Error: 'Power' data is missing in the payload from Antares.")
                return False

            new_reading = dbReading(
                DateTime=formatted_ts,
                Energy=content.get('Energy'),
                Power=content.get('Power'),
                Ampere=content.get('Current'),
                Voltage=content.get('Voltage'),
                Co2=content.get('TotalCO2'),
                Co2_cost=content.get('TotalCost')
            )
            db.session.add(new_reading)
            db.session.commit()
            print(f"SUCCESS: Stored data for {formatted_ts} UTC")
            return True

        except Exception as e:
            db.session.rollback()
            print(f"Store Error during Antares/DB operation for {formatted_ts} UTC: {e}")
            return False

def background_ntp_checker():
    """
    Checks the time and triggers a data fetch only during the last 5 minutes
    of every hour (e.g., from HH:55:00 to HH:59:59).
    """
    print("Background Checker started: Will fetch data in the last 5 minutes of each hour.")
    # Use a variable to track the last hour we successfully fetched data for.
    # Initialize to a value that will never match a real hour.
    last_successful_fetch_hour_utc = -1

    while True:
        # 1. Get a reliable UTC timestamp
        now_utc = get_ntp_time(NTP_SERVER)
        if not now_utc:
            print("NTP Error: Could not get time. Retrying in 60 seconds.")
            time.sleep(60)
            continue # Restart the loop

        current_hour = now_utc.hour
        current_minute = now_utc.minute

        # 2. Determine if we are allowed to fetch in the current hour
        has_fetched_this_hour = (current_hour == last_successful_fetch_hour_utc)

        # 3. Check if we are inside the fetch window (minutes 55-59)
        is_in_fetch_window = current_minute >= 55

        # 4. DECISION LOGIC: Fetch or Sleep?
        if is_in_fetch_window and not has_fetched_this_hour:
            # --- ATTEMPT TO FETCH ---
            # We are in the window and haven't successfully fetched for this hour yet.
            print(f"UTC {now_utc.strftime('%H:%M:%S')}: In fetch window. Attempting data retrieval.")

            was_successful = fetch_and_store_hourly_data(target_hour_dt_utc=now_utc)

            if was_successful:
                # On success, lock this hour and wait until the next hour's window.
                last_successful_fetch_hour_utc = current_hour
                print(f"Success for hour {current_hour}. Waiting for next hour's window.")
                # Sleep for ~55 minutes to completely miss the rest of this hour's window.
                time.sleep(55 * 60) 
            else:
                # On failure, wait a short interval and retry (the loop will come back here).
                print(f"Fetch failed. Retrying in {CHECK_INTERVAL_SECONDS} seconds.")
                time.sleep(CHECK_INTERVAL_SECONDS)
        else:
            # --- SLEEP UNTIL NEXT WINDOW ---
            # This block runs if:
            # a) It's NOT the fetch window (minute < 55)
            # b) We have ALREADY fetched for this hour.

            wait_seconds = 0
            # Calculate how many seconds to sleep until the next HH:55:00
            if current_minute < 55:
                # It's before the window, so wait until this hour's window starts.
                wait_seconds = (54 - current_minute) * 60 + (60 - now_utc.second)
            else:
                # It's after the window has started, but we've already fetched.
                # We need to wait for the *next* hour's window.
                seconds_remaining_in_hour = (59 - current_minute) * 60 + (60 - now_utc.second)
                wait_seconds = seconds_remaining_in_hour + (55 * 60)

            # Add a small buffer to ensure we land inside the window, not right at the edge.
            wait_seconds += 2
            
            next_fetch_time = now_utc + timedelta(seconds=wait_seconds)
            print(f"UTC {now_utc.strftime('%H:%M:%S')}: Not in fetch window or already completed. "
                  f"Sleeping for ~{round(wait_seconds / 60, 1)} minutes until {next_fetch_time.strftime('%Y-%m-%d %H:%M')}.")
            
            time.sleep(wait_seconds)


# --- Refactored Web Routes (Unchanged) ---

# Dictionary to map URL metric names to database columns and units
METRIC_CONFIG = {
    'energy': {'column': dbReading.Energy, 'unit': 'Wh'},
    'power': {'column': dbReading.Power, 'unit': 'W'},
    'ampere': {'column': dbReading.Ampere, 'unit': 'A'},
    'voltage': {'column': dbReading.Voltage, 'unit': 'V'},
    'co2': {'column': dbReading.Co2, 'unit': 'g'},
    'co2_cost': {'column': dbReading.Co2_cost, 'unit': 'IDR'}
}
ALLOWED_GRANULARITIES = ['hourly', 'daily', 'monthly']

@app.route('/')
def home():
    """Redirects the base URL to a default view."""
    return redirect(url_for('unified_view', metric='power', granularity='daily'))

@app.route('/view/<string:metric>/<string:granularity>')
def unified_view(metric, granularity):
    """A single route to display all data combinations."""
    if metric not in METRIC_CONFIG or granularity not in ALLOWED_GRANULARITIES:
        return "Error: Invalid metric or granularity specified.", 404

    config = METRIC_CONFIG[metric]
    metric_column = config['column']
    unit = config['unit']
    
    # Base query
    query = db.session.query(metric_column)
    
    # --- Data Aggregation ---
    if granularity == 'hourly':
        # For hourly, we just take the raw data points
        query = db.session.query(
            dbReading.DateTime,
            metric_column
        ).order_by(dbReading.DateTime.asc())
        results = query.all()
        
    elif granularity == 'daily':
        # Group by day and average the values
        query = db.session.query(
            func.strftime('%Y-%m-%d', dbReading.DateTime).label('date'),
            func.avg(metric_column).label('value')
        ).group_by('date').order_by('date')
        results = query.all()

    elif granularity == 'monthly':
        # Group by month and average the values
        query = db.session.query(
            func.strftime('%Y-%m', dbReading.DateTime).label('month'),
            func.avg(metric_column).label('value')
        ).group_by('month').order_by('month')
        results = query.all()

    # --- Prepare data for template ---
    # `results` is a list of tuples, e.g., ('2025-06-11', 150.5)
    labels = [row[0] for row in results]
    data_points = [round(row[1], 2) if row[1] is not None else 0 for row in results]
    
    # For the HTML table
    table_rows = [{"timestamp": row[0], "value": round(row[1], 2) if row[1] is not None else "N/A"} for row in reversed(results)]

    # Data to pass to the template
    context = {
        "title": f"{granularity.capitalize()} {metric.replace('_', ' ').title()}",
        "chart_labels": labels,
        "chart_data": data_points,
        "chart_unit": unit,
        "table_headers": [granularity.capitalize() + " Timestamp", f"Value ({unit})"],
        "table_rows": table_rows,
        "navigation": {
            "metrics": METRIC_CONFIG.keys(),
            "granularities": ALLOWED_GRANULARITIES,
            "current_metric": metric,
            "current_granularity": granularity
        }
    }

    return render_template('display.html', **context)

# --- Main Execution (Unchanged) ---
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print(f"Database '{DATABASE_FILE}' ensured/created.")

    fetch_thread = threading.Thread(target=background_ntp_checker, daemon=True)
    fetch_thread.start()

    print("\n--- Flask App Starting ---")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)