import os
import time
import threading
from datetime import datetime, timedelta, timezone
import socket
import struct
from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from antares_http import antares # Keep for Antares API

# --- Configuration ---
ANTARES_ACCESS_KEY = '5cd4cda046471a89:75f9e1c6b34bf41a' # Your Antares Key
ANTARES_PROJECT_NAME = 'UjiCoba_TA' # Your Antares Project
ANTARES_DEVICE_NAME = 'TA_DKT1' # Your Antares Device
DATABASE_FILE = 'mrp.db' # Renamed DB file
CHECK_INTERVAL_SECONDS = 15 # How often to check for new hour
LATITUDE = 14.5833 # Keep for temp fetching
LONGITUDE = 121.0 # Keep for temp fetching
API_TIMEZONE = "Asia/Kuala_Lumpur" # For internal processing if needed
NTP_SERVER = 'pool.ntp.org' # For reliable time checks



app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATABASE_FILE}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
app.jinja_env.globals['now'] = datetime.utcnow # For footer timestamp


class dbReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    DateTime = db.Column(db.String(19), nullable=False, unique=True, index=True)
    DailyEnergy = db.Column(db.Float) 
    Power = db.Column(db.Float, nullable=True) 

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

def fetch_and_store_hourly_data(target_hour_dt_utc: datetime):
    """Fetches Antares data and optional Power, stores hourly record if new."""
    with app.app_context():
        formatted_ts = target_hour_dt_utc.strftime('%Y-%m-%d %H:%M:00')

        if dbReading.query.filter_by(DateTime=formatted_ts).first():
            print(f"Hourly Store: Data for {formatted_ts} UTC already exists. Skipping.")
            return

        try:
            antares.setAccessKey(ANTARES_ACCESS_KEY)
            latest_data = antares.get(ANTARES_PROJECT_NAME, ANTARES_DEVICE_NAME)

            if not latest_data or 'content' not in latest_data:
                print(f"Hourly Store Error: Invalid or empty data received from Antares near {formatted_ts} UTC.")
                return

            content = latest_data['content']
            energy_wh = content.get('Power') 

            if energy_wh is None:
                print(f"Hourly Store Warning: Energy key not found in Antares data for {formatted_ts} UTC.")

            daily_energy = content.get('DailyEnergy')

            # Create and store new database record
            new_reading = dbReading(
                DateTime=formatted_ts,
                DailyEnergy=daily_energy, # Store None if energy was missing
                Power=energy_wh   # Store None if temp fetch failed
            )
            db.session.add(new_reading)
            db.session.commit()
            print(f"Stored Hourly: {formatted_ts} UTC - Energy(Wh): {energy_wh if energy_wh is not None else 'N/A'}, Energy: {daily_energy if daily_energy is not None else 'N/A'}")

        except Exception as e:
            db.session.rollback() # Rollback DB changes on error
            print(f"Hourly Store Error during Antares/DB operation for {formatted_ts} UTC: {e}")

def background_ntp_checker():
    """Periodically checks NTP time and triggers hourly fetch on hour change."""
    print("Background NTP Checker started...")
    previous_ntp_time_utc = None
    while True:
        current_ntp_time_utc = get_ntp_time(NTP_SERVER)
        if current_ntp_time_utc is not None:
            fetch_and_store_hourly_data(target_hour_dt_utc=current_ntp_time_utc)

        time.sleep(CHECK_INTERVAL_SECONDS)


@app.route('/')
def home():
    """Redirects the base URL to the database view."""
    return redirect(url_for('database_genergy_hourly')) # Changed redirect

# This API endpoint provides data for the JS chart updater
@app.route('/get_range_data')
def get_range_data():
    """API endpoint to provide data for the database graph based on date range."""
    start_date = request.args.get('start_date') # Expects YYYY-MM-DD
    end_date = request.args.get('end_date')     # Expects YYYY-MM-DD

    if not (start_date and end_date):
        return jsonify({"error": "Start and end dates are required."}), 400

    try:
        # Format for comparison with DateTime strings (YYYY-MM-DD HH:MM:SS)
        start_dt_str = f"{start_date} 00:00:00"
        end_dt_str = f"{end_date} 23:59:59"

        # Query the database for the specified range, order by time for the graph
        readings = dbReading.query.filter(
            dbReading.DateTime >= start_dt_str,
            dbReading.DateTime <= end_dt_str
        ).order_by(dbReading.DateTime).all()

        # Prepare data for Chart.js
        labels = [r.DateTime for r in readings]
        # Ensure energy/power data exists or provide default (e.g., null/0)
        energy_data = [r.DailyEnergy if r.DailyEnergy is not None else None for r in readings]
        power_data = [r.Power if r.Power is not None else None for r in readings] # Use null for missing power

        return jsonify({
            "labels": labels,
            "energyData": energy_data,
            "powerData": power_data
        })
    except ValueError:
         return jsonify({"error": "Invalid date format provided. Use YYYY-MM-DD."}), 400
    except Exception as e:
        print(f"Error in /get_range_data: {e}")
        return jsonify({"error": "Failed to retrieve data for the specified range."}), 500

    except ValueError:
        # Handle cases where the date format in the URL is invalid
        return "Invalid date format in URL. Use ISO format like YYYY-MM-DDTHH:MM:SS.", 400
    except Exception as e:
        # Catch other potential errors during database query or processing
        print(f"Error generating graph for range{e}")
        return "Error generating graph data.", 500
    
@app.route('/genergy_monthly')
def monthly_summary():
    all_readings = dbReading.query.order_by(dbReading.DateTime.asc()).all()
    data_for_js = [
        {
            "DateTime": r.DateTime,
            "Power": r.Power
        } for r in all_readings
    ]
    return render_template('genergy_monthly.html', readings=data_for_js)
    
@app.route('/genergy_daily')
def database_genergy_daily():
    # No need to pass readings here if the graph loads dynamically via JS
    # If you still want a table below the graph, keep fetching all_readings
    all_readings = dbReading.query.order_by(dbReading.DateTime.desc()).all()
    return render_template('genergy_daily.html', readings=all_readings) # Pass readings if table needed

@app.route('/genergy_hourly')
def database_genergy_hourly():
    """Displays the full data table and graph controls."""
    # Fetch all readings, ordered most recent first for the table display
    all_readings = dbReading.query.order_by(dbReading.DateTime.desc()).all()
    return render_template('genergy_hourly.html', readings=all_readings)

@app.route('/gpower_daily')
def database_gpower_daily():
    all_readings = dbReading.query.order_by(dbReading.DateTime.desc()).all()
    return render_template('gpower_daily.html', readings=all_readings)

@app.route('/gpower_hourly')
def database_gpower_hourly():
    all_readings = dbReading.query.order_by(dbReading.DateTime.desc()).all()
    return render_template('gpower_hourly.html', readings=all_readings)

@app.route('/database_text')
def database_text():
    all_readings = dbReading.query.order_by(dbReading.DateTime.desc()).all()
    return render_template('database_text.html', readings=all_readings)

# --- Main Execution ---
if __name__ == "__main__":
    # Ensure database exists when the app starts
    with app.app_context():
        db.create_all()
        print(f"Database '{DATABASE_FILE}' ensured/created.")

    # Start the background thread for fetching data
    fetch_thread = threading.Thread(target=background_ntp_checker, daemon=True)
    fetch_thread.start()

    print("\n--- Flask App Starting ---")
    print(f"Fetching Antares data for: {ANTARES_PROJECT_NAME}/{ANTARES_DEVICE_NAME}")
    print(f"Storing data in: {DATABASE_FILE}")
    print(f"Checking for new hour every {CHECK_INTERVAL_SECONDS} seconds.")
    print("Web Interface available at: http://localhost:5000 (or http://<your-ip>:5000)")
    print("--------------------------\n")

    # To expose this app using ngrok:
    # 3. After starting this Python script, open another terminal and run:
    #    ngrok http --url=sincere-moccasin-likely.ngrok-free.app 5000

    # Run the Flask development server
    # host='0.0.0.0' makes it accessible on your network
    # use_reloader=False is important when using threads
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)

