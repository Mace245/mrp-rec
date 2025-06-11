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
from zoneinfo import ZoneInfo # For timezone conversion

# --- Initialize extensions without connecting to an app ---
db = SQLAlchemy()

# --- Updated Database Model ---
# The 'DailyEnergy' column has been renamed to 'Energy'
class dbReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    DateTime = db.Column(db.String(25), nullable=False, unique=True, index=True)
    Energy = db.Column(db.Float)
    Power = db.Column(db.Float, nullable=True)
    Ampere = db.Column(db.Float, nullable=True)
    Voltage = db.Column(db.Float, nullable=True)
    Co2 = db.Column(db.Float, nullable=True)
    Co2_cost = db.Column(db.Float, nullable=True)

# --- Configuration (loaded inside the factory) ---
# Secrets and environment-specific variables are read using os.getenv
ANTARES_ACCESS_KEY = os.getenv('ANTARES_ACCESS_KEY', 'YOUR_DEFAULT_KEY')
ANTARES_PROJECT_NAME = 'TADKT-1'
ANTARES_DEVICE_NAME = 'PMM'
DATABASE_FILE = 'mrp.db' # Fallback for local development
CHECK_INTERVAL_SECONDS = 15
NTP_SERVER = 'pool.ntp.org'

# --- Updated METRIC_CONFIG ---
# Reflects the new 'Energy' column and updated Antares keys
METRIC_CONFIG = {
    'energy': {'column': dbReading.Energy, 'unit': 'Wh'},
    'power': {'column': dbReading.Power, 'unit': 'W'},
    'ampere': {'column': dbReading.Ampere, 'unit': 'A'},
    'voltage': {'column': dbReading.Voltage, 'unit': 'V'},
    'co2': {'column': dbReading.Co2, 'unit': 'g'},
    'co2_cost': {'column': dbReading.Co2_cost, 'unit': 'IDR'}
}
ALLOWED_GRANULARITIES = ['hourly', 'daily', 'monthly']

# --- Application Factory Function (The recommended structure) ---
def create_app():
    """Creates and configures the Flask app, ensuring robust startup."""
    app = Flask(__name__)
    app.jinja_env.globals['now'] = datetime.utcnow

    # Configuration is done inside the factory to avoid timing issues
    database_url = os.getenv('DATABASE_URL')
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    # Use Railway's PostgreSQL DB if available, otherwise use local SQLite file
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url or f'sqlite:///{DATABASE_FILE}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Connect the SQLAlchemy object to the configured app
    db.init_app(app)

    # --- Routes ---
    @app.route('/')
    def home():
        return redirect(url_for('unified_view', metric='power', granularity='daily'))

    @app.route('/view/<string:metric>/<string:granularity>')
    def unified_view(metric, granularity):
        if metric not in METRIC_CONFIG or granularity not in ALLOWED_GRANULARITIES:
            return "Error: Invalid metric or granularity specified.", 404

        config = METRIC_CONFIG[metric]
        metric_column = config['column']
        unit = config['unit']
        
        # Data aggregation queries remain the same logic
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

# --- Data Fetching Logic ---
def get_ntp_time(server="pool.ntp.org"):
    NTP_PORT, NTP_PACKET_FORMAT, NTP_DELTA = 123, "!12I", 2208988800
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(5)
            client.sendto(b'\x1b' + 47 * b'\0', (server, NTP_PORT))
            data, _ = client.recvfrom(1024)
            if data:
                secs = struct.unpack(NTP_PACKET_FORMAT, data)[10]
                return datetime.fromtimestamp(secs - NTP_DELTA, timezone.utc)
    except Exception as e:
        print(f"NTP Error: {e}")
    return None

def fetch_and_store_data(app):
    """Fetches data from Antares and stores it in the database."""
    with app.app_context(): # Essential for database operations in a thread
        try:
            antares.setAccessKey(ANTARES_ACCESS_KEY)
            latest_data = antares.get(ANTARES_PROJECT_NAME, ANTARES_DEVICE_NAME)

            if not latest_data or 'content' not in latest_data:
                return

            content = latest_data['content']
            
            # Use current UTC time for a consistent timestamp
            timestamp_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')

            # Check if this exact timestamp already exists to prevent duplicates
            if dbReading.query.filter_by(DateTime=timestamp_utc).first():
                return
            
            # Updated to use the new keys from your latest code
            new_reading = dbReading(
                DateTime=timestamp_utc,
                Energy=content.get('Energy'),
                Power=content.get('Power'),
                Ampere=content.get('Current'), # Using 'Current' as in your new code
                Voltage=content.get('Voltage'),
                Co2=content.get('TotalCO2'),      # Using 'TotalCO2'
                Co2_cost=content.get('TotalCost') # Using 'TotalCost'
            )
            db.session.add(new_reading)
            db.session.commit()
            print(f"Stored: Data for {timestamp_utc}")
        except Exception as e:
            db.session.rollback()
            print(f"Store Error during Antares/DB operation: {e}")

def background_checker(app):
    """Background thread to periodically fetch and store data."""
    print("Background Checker started...")
    while True:
        fetch_and_store_data(app)
        time.sleep(CHECK_INTERVAL_SECONDS)

# --- Main Execution Block ---
# Create the app instance using the factory
app = create_app()

if __name__ == "__main__":
    with app.app_context():
        # This creates the tables in the PostgreSQL database if they don't exist
        db.create_all()
        print(f"Database tables ensured on: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

    # Start the background data-fetching thread
    fetch_thread = threading.Thread(target=background_checker, args=(app,), daemon=True)
    fetch_thread.start()

    print("--- Flask App Starting ---")
    # debug=False is crucial for production performance and stability
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)

