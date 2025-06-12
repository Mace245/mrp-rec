import os
import time
import threading
from datetime import datetime, timezone
import socket
import struct
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from antares_http import antares
from zoneinfo import ZoneInfo

# --- Initialize extensions without connecting to an app (Factory Pattern) ---
db = SQLAlchemy()

# --- Database Model ---
# Added 'HourlyEnergy' and removed 'Energy'.
class dbReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    DateTime = db.Column(db.String(25), nullable=False, unique=True, index=True) # YYYY-MM-DD HH:00:00
    HourlyEnergy = db.Column(db.Float) # This will now store the PEAK value for the hour
    Power = db.Column(db.Float, nullable=True)
    Ampere = db.Column(db.Float, nullable=True)
    Voltage = db.Column(db.Float, nullable=True)
    Co2 = db.Column(db.Float, nullable=True)
    Co2_cost = db.Column(db.Float, nullable=True)

# --- Configuration ---
ANTARES_ACCESS_KEY = os.getenv('ANTARES_ACCESS_KEY', 'YOUR_DEFAULT_KEY')
ANTARES_PROJECT_NAME = 'TADKT-1'
ANTARES_DEVICE_NAME = 'PMM'
DATABASE_FILE = 'mrp.db'
CHECK_INTERVAL_SECONDS = 15 # Fetch data more frequently to catch peaks
NTP_SERVER = 'pool.ntp.org'

# --- Metric Configuration for UI ---
# Replaced 'energy' with 'hourly_energy'
METRIC_CONFIG = {
    'hourly_energy': {'column': dbReading.HourlyEnergy, 'unit': 'Wh'},
    'power': {'column': dbReading.Power, 'unit': 'W'},
    'ampere': {'column': dbReading.Ampere, 'unit': 'A'},
    'voltage': {'column': dbReading.Voltage, 'unit': 'V'},
    'co2': {'column': dbReading.Co2, 'unit': 'g'},
    'co2_cost': {'column': dbReading.Co2_cost, 'unit': 'IDR'}
}
ALLOWED_GRANULARITIES = ['hourly', 'daily', 'monthly']

# --- Application Factory Function ---
def create_app():
    app = Flask(__name__)
    app.jinja_env.globals['now'] = datetime.utcnow

    # Configure database URI safely
    database_url = os.getenv('DATABASE_URL')
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url or f'sqlite:///{DATABASE_FILE}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)

    @app.cli.command("init-db")
    def init_db_command():
        """Creates the database tables."""
        db.create_all()
        print("Initialized the database and created tables.")

    # --- Routes ---
    @app.route('/')
    def home():
        return redirect(url_for('unified_view', metric='hourly_energy', granularity='daily'))

    @app.route('/view/<string:metric>/<string:granularity>')
    def unified_view(metric, granularity):
        if metric not in METRIC_CONFIG or granularity not in ALLOWED_GRANULARITIES:
            return "Error: Invalid metric or granularity specified.", 404

        config = METRIC_CONFIG[metric]
        metric_column = config['column']
        unit = config['unit']
        
        # Aggregation logic remains the same
        if granularity == 'hourly':
            query = db.session.query(dbReading.DateTime, metric_column).order_by(dbReading.DateTime.asc())
        elif granularity == 'daily':
            query = db.session.query(func.strftime('%Y-%m-%d', dbReading.DateTime).label('date'), func.avg(metric_column).label('value')).group_by('date').order_by('date')
        else: # monthly
            query = db.session.query(func.strftime('%Y-%m', dbReading.DateTime).label('month'), func.avg(metric_column).label('value')).group_by('month').order_by('month')

        results = query.all()
        labels = [row[0] for row in results]
        data_points = [round(row[1], 2) if row[1] is not None else 0 for row in results]
        table_rows = [{"timestamp": row[0], "value": round(row[1], 2) if row[1] is not None else "N/A"} for row in reversed(results)]

        context = { "title": f"{granularity.capitalize()} {metric.replace('_', ' ').title()}", "chart_labels": labels, "chart_data": data_points, "chart_unit": unit, "table_headers": [granularity.capitalize() + " Timestamp", f"Value ({unit})"], "table_rows": table_rows, "navigation": { "metrics": METRIC_CONFIG.keys(), "granularities": ALLOWED_GRANULARITIES, "current_metric": metric, "current_granularity": granularity }}
        return render_template('display.html', **context)

    return app

# --- Data Fetching & Peak Logic ---

# Global state for tracking the current hour's data
current_tracking_hour = None
current_peak_energy = -1.0
current_db_record = None

def fetch_and_process_peak_data(app):
    """Fetches data and updates/creates records based on peak hourly energy."""
    global current_tracking_hour, current_peak_energy, current_db_record

    with app.app_context():
        try:
            antares.setAccessKey(ANTARES_ACCESS_KEY)
            latest_data = antares.get(ANTARES_PROJECT_NAME, ANTARES_DEVICE_NAME)

            if not latest_data or 'content' not in latest_data: return
            content = latest_data['content']

            if content.get('timestamp'):
                print('diff format')
                return
            
            new_hourly_energy = content.get('HourlyEnergy')
            if new_hourly_energy is None: return # Skip if there's no energy data

            # Get the current time and truncate to the start of the hour
            now_utc = datetime.now(timezone.utc)
            hour_start_utc = now_utc.replace(minute=0, second=0, microsecond=0)
            hour_start_str = hour_start_utc.strftime('%Y-%m-%d %H:%M:%S')

            # --- CORE LOGIC ---
            # Scenario 1: The hour has changed
            if hour_start_utc != current_tracking_hour:
                print(f"New Hour Detected: {hour_start_str}. Finalized last hour's peak.")
                current_tracking_hour = hour_start_utc
                current_peak_energy = float(new_hourly_energy)
                
                # Create a new record for this new hour
                new_record = dbReading(
                    DateTime=hour_start_str,
                    HourlyEnergy=current_peak_energy,
                    Power=content.get('Power'),
                    Ampere=content.get('Current'),
                    Voltage=content.get('Voltage'),
                    Co2=content.get('TotalCO2'),
                    Co2_cost=content.get('TotalCost')
                )
                db.session.add(new_record)
                db.session.commit()
                current_db_record = new_record # Keep track of the new record
                print(f"CREATED new record for {hour_start_str} with peak energy: {current_peak_energy}")

            # Scenario 2: Same hour, check if we found a new peak
            elif float(new_hourly_energy) > current_peak_energy:
                current_peak_energy = float(new_hourly_energy)
                if current_db_record:
                    # Update the existing record for this hour with the new peak value
                    current_db_record.HourlyEnergy = current_peak_energy
                    # Optionally update other metrics to reflect the state at the peak
                    current_db_record.Power = content.get('Power')
                    current_db_record.Ampere = content.get('Current')
                    db.session.commit()
                    print(f"UPDATED record for {hour_start_str} with new peak energy: {current_peak_energy}")

        except Exception as e:
            db.session.rollback()
            print(f"Store Error during peak processing: {e}")

def background_checker(app):
    """Background thread to periodically fetch and process data."""
    print("Background Peak Checker started...")
    while True:
        fetch_and_process_peak_data(app)
        time.sleep(CHECK_INTERVAL_SECONDS)

# --- Main Execution Block ---
app = create_app()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print(f"Local database tables ensured on: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    fetch_thread = threading.Thread(target=background_checker, args=(app,), daemon=True)
    fetch_thread.start()
    
    print("--- Flask App Starting Locally ---")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)

