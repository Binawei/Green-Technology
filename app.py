import random
import secrets
import string
from functools import wraps
import datetime
import logging

import click
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, session, g)
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

# --- Import db and Models ---
from models import db, Greenhouse, Issue, Employee, EnvironmentalData

# --- Import Config ---
from config import get_config

# --- Import Utils ---
try:
    from utils import (check_temperature, check_humidity, check_co2,
                       check_light_intensity, check_soil_ph, check_soil_moisture,
                       send_email_notification)
except ImportError:
    logging.error("Could not import from utils.py.")


# --- Create and Configure App ---
app = Flask(__name__)

# --- Load Configuration based on FLASK_ENV ---
#
app_config = get_config()
app.config.from_object(app_config)

# --- Initialize Extensions ---
db.init_app(app)


# --- Logging Setup (using config value) ---
log_level_name = app.config.get('LOGGING_LEVEL', 'INFO')
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level,
                    format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'employee_id' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


@app.before_request
def load_logged_in_user():
    employee_id = session.get('employee_id')
    if employee_id is None:
        g.employee = None
    else:
        g.employee = Employee.query.get(employee_id)
        if g.employee is None:
             session.clear()
             flash("Your session was invalid. Please log in again.", "error")

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'employee_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        employee = Employee.query.filter_by(email=email).first()

        if employee and employee.check_password(password):
            session.clear()
            session['employee_id'] = employee.id
            session['employee_name'] = employee.name
            session['is_admin'] = employee.is_admin
            flash(f"Welcome back, {employee.name}!", "success")

            next_page = request.args.get('next')
            if next_page and next_page.startswith('/') and not next_page.startswith('//'):
                 return redirect(next_page)
            else:
                 return redirect(url_for('dashboard'))
        else:
            # Incorrect login
            flash("Invalid email or password. Please try again.", "error")

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    employee_name = session.get('employee_name', 'User')
    session.clear()
    flash(f"You have been successfully logged out, {employee_name}.", "info")
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    try:
        all_greenhouses_query = Greenhouse.query.options(
            selectinload(Greenhouse.issues),
            # selectinload(Greenhouse.environmental_data) # Avoid loading ALL data
        ).order_by(Greenhouse.id) # Keep a consistent order initially

        all_greenhouses = all_greenhouses_query.all()

        greenhouse_display_data = []
        for gh in all_greenhouses:
            has_ongoing_issue = any(issue.status == 'Ongoing' for issue in gh.issues)
            status_text = 'Issue Detected' if has_ongoing_issue else 'Normal'
            status_class = 'issue-detected' if has_ongoing_issue else 'normal'

            latest_data = EnvironmentalData.query.filter_by(
                greenhouse_id=gh.id
            ).order_by(
                EnvironmentalData.timestamp.desc()
            ).first()

            greenhouse_display_data.append({
                'greenhouse': gh,
                'status_text': status_text,
                'status_class': status_class,
                'has_ongoing_issue': has_ongoing_issue,
                'latest_data': latest_data
            })


        def sort_key(gh_data):
            return (0 if gh_data['has_ongoing_issue'] else 1, gh_data['greenhouse'].id)

        sorted_greenhouses_data = sorted(greenhouse_display_data, key=sort_key)
        displayed_greenhouses_data = sorted_greenhouses_data[:4] # Slice after sorting
        # --- End Sorting ---

        # --- Existing Counts ---
        employee_count = Employee.query.count()
        ongoing_issue_count = sum(1 for gh_data in greenhouse_display_data if gh_data['has_ongoing_issue'])
        resolved_issue_count = db.session.query(func.count(Issue.id)).filter(Issue.status == 'Resolved').scalar() or 0

        # --- Check for personal ongoing issue for alert modal ---
        assigned_greenhouse_issue = None
        if g.employee and g.employee.greenhouse_id:
            assigned_greenhouse_issue = Issue.query.options(
                joinedload(Issue.originating_greenhouse)
            ).filter(
                Issue.greenhouse_id == g.employee.greenhouse_id,
                Issue.status == 'Ongoing'
            ).order_by(
                Issue.created_at.desc()
            ).first()

        return render_template('dashboard.html',
                               displayed_greenhouses_data=displayed_greenhouses_data,
                               ongoing_issue_count=ongoing_issue_count,
                               resolved_issue_count=resolved_issue_count,
                               employee_count=employee_count,
                               assigned_greenhouse_issue=assigned_greenhouse_issue)

    except Exception as e:
        app.logger.error(f"Dashboard error: {e}", exc_info=True)
        flash("An unexpected error occurred loading the dashboard.", "error")
        return render_template('dashboard.html', displayed_greenhouses_data=[], ongoing_issue_count=0, resolved_issue_count=0, employee_count=0, assigned_greenhouse_issue=None)



# !!! IMPORTANT: You will need to create this route and template later !!!
@app.route('/issues')
@login_required
def view_all_issues():
    """Displays a list of all issues, both ongoing and resolved."""
    try:

        issues_query = Issue.query.options(
            joinedload(Issue.originating_greenhouse)
        ).order_by(
            Issue.status.asc(),
            Issue.created_at.desc()
        )

        # Apply filtering based on user role/assignment (Optional example)
        if not g.employee.is_admin:
            # Non-admins only see issues for their assigned greenhouse
            if g.employee.assigned_greenhouse:
                issues_query = issues_query.filter(Issue.greenhouse_id == g.employee.greenhouse_id)
            else:
                issues = [] # Or handle appropriately if they can see unassigned?
                flash("You are not assigned to a greenhouse to view issues.", "warning")
                return render_template('all_issues.html', issues=issues)

        issues = issues_query.all()

        return render_template('all_issues.html', issues=issues)

    except Exception as e:
        if "exist" in str(e) or "relation" in str(e).lower():
             flash("Database tables might be missing or not fully migrated. Run 'flask db upgrade'.", "error")
             return render_template('all_issues.html', issues=[]) # Render empty list
        else:
             app.logger.error(f"View all issues error: {e}", exc_info=True)
             flash("An unexpected error occurred loading issues.", "error")
             return render_template('all_issues.html', issues=[]) # Render empty list




@app.route('/greenhouses')
@login_required
def view_greenhouses():
    """Displays a list of all greenhouses."""
    try:
        # Query greenhouses, perhaps add counts later
        greenhouses = Greenhouse.query.order_by(Greenhouse.name).all()
        return render_template('view_greenhouses.html', greenhouses=greenhouses)
    except Exception as e:
         # Handle potential DB errors
         if "exist" in str(e) or "relation" in str(e).lower():
             flash("Database tables might be missing or not fully migrated. Run 'flask db upgrade'.", "error")
             return render_template('view_greenhouses.html', greenhouses=[]) # Render empty list
         else:
             app.logger.error(f"View greenhouses error: {e}", exc_info=True)
             flash("An unexpected error occurred loading greenhouses.", "error")
             return render_template('view_greenhouses.html', greenhouses=[]) # Render empty list




@app.route('/input/<int:greenhouse_id>', methods=['GET', 'POST'])
@login_required
def input_form(greenhouse_id):
    greenhouse = Greenhouse.query.get_or_404(greenhouse_id)
    if request.method == 'POST':
        issue_created = False
        notifications_to_send = []
        issue_description_parts = []

        try:
            temperature = float(request.form['temperature'])
            humidity = float(request.form['humidity'])
            co2 = float(request.form['co2'])
            light_intensity = float(request.form['light_intensity'])
            soil_ph = float(request.form['soil_ph'])
            soil_moisture = float(request.form['soil_moisture'])

            # --- Create Environmental Data Entry ---
            new_data = EnvironmentalData(
                greenhouse_id=greenhouse_id,
                temperature=temperature, humidity=humidity, co2=co2,
                light_intensity=light_intensity, soil_ph=soil_ph,
                soil_moisture=soil_moisture,
                timestamp=datetime.datetime.utcnow(),
                source='manual'
            )
            db.session.add(new_data)

            # --- Check Environmental Conditions & Prepare Issue/Notification ---
            if not check_temperature(temperature):
                issue_description_parts.append(f"Temperature {temperature}°C is out of range (20-25°C).")
            if not check_humidity(humidity):
                issue_description_parts.append(f"Humidity {humidity}% is out of range (40-60%).")
            if not check_co2(co2):
                issue_description_parts.append(f"CO2 {co2} ppm is out of range (400-1000 ppm).")
            if not check_light_intensity(light_intensity):
                issue_description_parts.append(f"Light Intensity {light_intensity} lux is out of range (1000-10000 lux).")
            if not check_soil_ph(soil_ph):
                issue_description_parts.append(f"Soil pH {soil_ph} is out of range (6.0-7.0).")
            if not check_soil_moisture(soil_moisture):
                issue_description_parts.append(f"Soil Moisture {soil_moisture}% is out of range (30-60%).")

            # --- If any issues were found ---
            if issue_description_parts:
                issue_created = True
                full_issue_description = f"Alert for Greenhouse '{greenhouse.name}' ({greenhouse.location}):\n- " + "\n- ".join(issue_description_parts)

                # Create Issue in DB
                new_issue = Issue(
                    greenhouse_id=greenhouse_id,
                    description=full_issue_description,
                    status='Ongoing'
                )
                db.session.add(new_issue)

                # Prepare email notification details
                subject = f"Alert: GreenHouse Notification For '{greenhouse.name}'"
                # Find employees assigned to THIS greenhouse
                employees_to_notify = greenhouse.employees # Use the relationship
                recipient_emails = [emp.email for emp in employees_to_notify if emp.email and emp.available]

                if recipient_emails:
                     notifications_to_send.append({
                         'subject': subject,
                         'recipients': recipient_emails,
                         'body': full_issue_description + "\n\nPlease investigate and resolve the issue."
                     })
                else:
                    app.logger.warning(f"Issue detected in Greenhouse '{greenhouse.name}', but no active employees with emails are assigned.")


            # --- Commit DB changes (Data and potentially Issue) ---
            db.session.commit()
            flash("Data recorded successfully!", "success")
            if issue_created:
                pass
                 # flash("An issue was detected and logged.", "warning")

        except ValueError:
            db.session.rollback() # Rollback if data conversion fails
            flash("Invalid input. Please ensure all fields are numbers.", "error")
            return render_template('input_form.html', greenhouse_id=greenhouse_id, greenhouse_name=greenhouse.name)
        except Exception as e:
            db.session.rollback() # Rollback on any other error during DB operations
            app.logger.error(f"Error saving data/issue for G{greenhouse_id}: {e}", exc_info=True)
            flash("An error occurred while saving data or logging the issue.", "error")
            return render_template('input_form.html', greenhouse_id=greenhouse_id, greenhouse_name=greenhouse.name)

        # --- Send Notifications AFTER successful DB commit ---
        if notifications_to_send:
            for notification in notifications_to_send:

                 email_sent = send_email_notification(
                     subject=notification['subject'],
                     recipients=notification['recipients'],
                     body=notification['body']
                 )
                 if not email_sent:
                      flash(f"Alert logged, but failed to send email notification to {', '.join(notification['recipients'])}. Please check system logs.", "danger")

        return redirect(url_for('input_form', greenhouse_id=greenhouse_id))

    # GET request
    return render_template('input_form.html', greenhouse_id=greenhouse_id, greenhouse_name=greenhouse.name)




@app.route('/issue/resolve/<int:issue_id>', methods=['POST'], endpoint='resolve_issue')
@login_required
def resolve_issue(issue_id):
    issue = Issue.query.options(joinedload(Issue.originating_greenhouse)).get_or_404(issue_id)
    greenhouse_id_affected = issue.greenhouse_id

    # Permission Check...
    can_resolve = False
    if g.employee.is_admin: can_resolve = True
    elif issue.originating_greenhouse and g.employee.greenhouse_id and issue.greenhouse_id == g.employee.greenhouse_id: can_resolve = True
    if not can_resolve:
        flash("You do not have permission to resolve this issue.", "error")
        return redirect(request.referrer or url_for('dashboard'))

    if issue.status == 'Ongoing':
        try:
            issue.status = 'Resolved'
            issue.resolved_at = datetime.datetime.utcnow()

            # Log a "Normal" data point using values from config
            normal_data = EnvironmentalData(
                greenhouse_id=greenhouse_id_affected,
                temperature=app.config.get('NORMAL_TEMP', 22.5),
                humidity=app.config.get('NORMAL_HUMIDITY', 50.0),
                co2=app.config.get('NORMAL_CO2', 700.0),
                light_intensity=app.config.get('NORMAL_LIGHT', 5000.0),
                soil_ph=app.config.get('NORMAL_PH', 6.5),
                soil_moisture=app.config.get('NORMAL_MOISTURE', 45.0),
                timestamp=datetime.datetime.utcnow(),
                source='resolution'
            )
            db.session.add(normal_data)
            db.session.commit()
            flash(f"Issue #{issue.id} marked as resolved. Normal environmental state logged.", "success")

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error resolving issue {issue_id}: {e}", exc_info=True)
            flash("An error occurred while resolving the issue.", "error")
    else:
        flash(f"Issue #{issue.id} was already resolved.", "info")
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/historical_data')
@login_required
def historical_data():
    try:
        data_query = EnvironmentalData.query.filter_by(source='manual').options(
            joinedload(EnvironmentalData.greenhouse)
        ).order_by(EnvironmentalData.timestamp.desc())

        page = request.args.get('page', 1, type=int)
        pagination = data_query.paginate(page=page, per_page=20, error_out=False)
        data = pagination.items
        return render_template('historical_data.html', data=data, pagination=pagination)

    except Exception as e:
        if "exist" in str(e).lower() or "relation" in str(e).lower() or "column" in str(e).lower():
             flash("Database tables/columns might be missing or not fully migrated. Run 'flask db upgrade'.", "error")
             return render_template('historical_data.html', data=[])
        else:
             app.logger.error(f"Historical data error: {e}", exc_info=True)
             flash("An unexpected error occurred loading historical data.", "error")
             return render_template('historical_data.html', data=[])



@app.route('/api/greenhouse/<int:greenhouse_id>/latest_data')
@login_required
def get_greenhouse_latest_data(greenhouse_id):
    greenhouse = Greenhouse.query.get_or_404(greenhouse_id)
    latest_data = EnvironmentalData.query.filter_by(
        greenhouse_id=greenhouse_id
    ).order_by(
        EnvironmentalData.timestamp.desc()
    ).first()

    if latest_data:
        data_dict = {
            'temperature': latest_data.temperature,
            'humidity': latest_data.humidity,
            'co2': latest_data.co2,
            'light_intensity': latest_data.light_intensity,
            'soil_ph': latest_data.soil_ph,
            'soil_moisture': latest_data.soil_moisture,
            # Format timestamp for better JS handling
            'timestamp': latest_data.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')
        }
    else:
        data_dict = None

    return jsonify({
        'success': True,
        'greenhouse_name': greenhouse.name,
        'location': greenhouse.location,
        'data': data_dict
    })


@app.route('/create_greenhouse', methods=['GET', 'POST'])
@login_required
def create_greenhouse():
    if request.method == 'POST':
        name = request.form['name']
        location = request.form['location']
        new_greenhouse = Greenhouse(name=name, status='normal', issue_description=None, location=location)
        db.session.add(new_greenhouse)
        db.session.commit()
        flash("Greenhouse created successfully!", "success")
        return redirect(url_for('dashboard'))

    return render_template('create_greenhouse.html')



@app.route('/create_employee', methods=['GET', 'POST'])
@login_required
def create_employee():
    if not g.employee or not g.employee.is_admin:
       flash("You do not have permission to create new employees.", "error")
       return redirect(url_for('dashboard'))

    greenhouses = Greenhouse.query.order_by(Greenhouse.name).all()
    if request.method == 'POST':
        new_password = None
        name = request.form.get('name')
        email = request.form.get('email')

        try:
            greenhouse_id_str = request.form.get('greenhouse_id')
            is_admin_form = 'is_admin' in request.form

            # --- Validation ---
            if not name or not email:
                flash("Name and email are required.", "warning")
                return render_template('create_employee.html', greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)
            if Employee.query.filter_by(email=email).first():
                 flash("An employee with this email already exists.", "error")
                 return render_template('create_employee.html', greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)

            greenhouse_id = None
            if greenhouse_id_str and greenhouse_id_str.isdigit():
                try:
                    potential_id = int(greenhouse_id_str)
                    if Greenhouse.query.get(potential_id):
                         greenhouse_id = potential_id
                    else:
                         flash("Selected greenhouse does not exist.", "error")
                         return render_template('create_employee.html', greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)
                except ValueError:
                     flash("Invalid greenhouse selection.", "error")
                     return render_template('create_employee.html', greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)
            elif greenhouse_id_str:
                greenhouse_id = None

            new_password = generate_password()
            company_id = generate_unique_company_id()

            new_employee = Employee(
                name=name,
                email=email,
                available=True,
                greenhouse_id=greenhouse_id,
                company_id=company_id,
                is_admin=is_admin_form
            )
            new_employee.set_password(new_password)

            db.session.add(new_employee)
            db.session.commit()
            # --- End Database Operations ---

            log_message = f"CREATED Employee: Name='{name}', Email='{email}', CompanyID='{company_id}', Admin={is_admin_form}"
            app.logger.info(log_message)
            print(f"!!! DEBUG ONLY - CREATED Employee: Name='{name}', Email='{email}', CompanyID='{company_id}', TempPassword='{new_password}', Admin={is_admin_form}")


            # --- Send Welcome Email ---
            email_subject = "Welcome to GreenTech Monitoring - Your Account Details"
            email_body = f"""Hello {name},

            Welcome to the GreenTech Monitoring System!
            Your account has been created successfully.
            
            You can log in using the following credentials:
            Email: {email}
            Temporary Password: {new_password}
            
            Please log in at http://127.0.0.1:5000/
            
            We strongly recommend changing this password via your profile settings after your first login for security reasons.
            
            Regards,
            The GreenTech Team
            """

            if app.config.get('MAIL_ENABLED'):
                email_sent = send_email_notification(
                    subject=email_subject,
                    recipients=[email],
                    body=email_body
                )

                if email_sent:
                    flash(f"Employee '{name}' created successfully! Credentials have been emailed to {email}.", "success")
                else:
                    flash(f"Employee '{name}' created successfully, BUT failed to send the welcome email.", "warning")
                    flash(f"Please manually provide the password to the user: {new_password}", "info") # Show password here if email fails
            else:
                 flash(f"Employee '{name}' created successfully! Email notifications are disabled.", "info")
                 flash(f"Please manually provide the password to the user: {new_password}", "info")

            return redirect(url_for('view_employees'))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error creating employee: {e}", exc_info=True)
            flash(f"An error occurred while creating the employee: {e}", "error")
            return render_template('create_employee.html', greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)

    return render_template('create_employee.html', greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)


@app.route('/view_employees')
@login_required
def view_employees():
    try:
        employees = Employee.query.options(db.joinedload(Employee.assigned_greenhouse)).order_by(Employee.name).all()
        return render_template('view_employee.html', employees=employees)
    except AttributeError as ae:

        app.logger.error(f"View employees Attribute error: {ae}", exc_info=True)
        flash("Error accessing employee relationship. Check model backref names.", "error")
        return render_template('view_employee.html', employees=[])
    except Exception as e:
         if "exist" in str(e) or "relation" in str(e).lower():
             flash("Database tables not found. Run 'flask init-db'.", "error")
             return render_template('view_employee.html', employees=[])
         else:
             app.logger.error(f"View employees error: {e}", exc_info=True)
             flash("An unexpected error occurred viewing employees.", "error")
             return render_template('view_employee.html', employees=[])


@app.route('/api/employee/<int:employee_id>')
@login_required
def api_employee_details(employee_id):
    employee = Employee.query.options(db.joinedload(Employee.assigned_greenhouse)).get(employee_id)
    if employee:
        return jsonify({
            'id': employee.id,
            'name': employee.name,
            'email': employee.email,
            'company_id': employee.company_id,
            'available': employee.available,
            'is_admin': employee.is_admin, # Include admin status
            'greenhouse': {
                'id': employee.assigned_greenhouse.id,
                'name': employee.assigned_greenhouse.name,
                'location': employee.assigned_greenhouse.location
            } if employee.assigned_greenhouse else None
        })
    else:
        return jsonify({'error': 'Employee not found'}), 404


def generate_unique_company_id():
    while True:
        random_number = random.randint(100000, 999999)
        company_id = f"GT{random_number}"
        if not Employee.query.filter_by(company_id=company_id).first():
            return company_id


@app.route('/employee/edit/<int:employee_id>', methods=['GET', 'POST'])
@login_required
def edit_employee(employee_id):

    employee_to_edit = Employee.query.get_or_404(employee_id)
    if not g.employee.is_admin and g.employee.id != employee_to_edit.id:
         flash("You do not have permission to edit this employee.", "error")
         return redirect(url_for('view_employees'))

    greenhouses = Greenhouse.query.order_by(Greenhouse.name).all()

    if request.method == 'POST':
        try:
            employee_to_edit.name = request.form.get('name')
            new_email = request.form.get('email')
            new_greenhouse_id_str = request.form.get('greenhouse_id')
            employee_to_edit.available = 'available' in request.form
            if g.employee.is_admin:
                 employee_to_edit.is_admin = 'is_admin' in request.form

            if not employee_to_edit.name or not new_email:
                 flash("Name and email cannot be empty.", "warning")
                 return render_template('edit_employee.html', employee=employee_to_edit, greenhouses=greenhouses)

            if new_email != employee_to_edit.email:
                 existing_user = Employee.query.filter(Employee.email == new_email, Employee.id != employee_to_edit.id).first()
                 if existing_user:
                     flash("Another employee is already using that email address.", "error")
                     return render_template('edit_employee.html', employee=employee_to_edit, greenhouses=greenhouses)
                 employee_to_edit.email = new_email

            new_greenhouse_id = None
            if new_greenhouse_id_str and new_greenhouse_id_str != 'None':
                try:
                    new_greenhouse_id = int(new_greenhouse_id_str)
                    if not Greenhouse.query.get(new_greenhouse_id):
                         raise ValueError("Invalid greenhouse selected.")
                except ValueError as ve:
                     flash(str(ve), "error")
                     return render_template('edit_employee.html', employee=employee_to_edit, greenhouses=greenhouses)
            employee_to_edit.greenhouse_id = new_greenhouse_id


            db.session.commit()
            flash(f"Employee '{employee_to_edit.name}' updated successfully!", "success")
            return redirect(url_for('view_employees'))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error editing employee {employee_id}: {e}", exc_info=True)
            flash("An error occurred while updating the employee.", "error")
            # Re-render edit form with potentially modified (but not saved) data on error
            return render_template('edit_employee.html', employee=employee_to_edit, greenhouses=greenhouses)


    return render_template('edit_employee.html', employee=employee_to_edit, greenhouses=greenhouses, current_user_is_admin=g.employee.is_admin)


# @app.route('/issue/resolve/<int:issue_id>', methods=['POST'])
# @login_required
# def resolve_issues(issue_id):
#     issue = Issue.query.get_or_404(issue_id)
#
#     can_resolve = False
#     if g.employee.is_admin:
#         can_resolve = True
#     elif issue.originating_greenhouse and g.employee.assigned_greenhouse and issue.originating_greenhouse.id == g.employee.assigned_greenhouse.id:
#          can_resolve = True
#
#     if not can_resolve:
#          flash("You do not have permission to resolve this issue.", "error")
#          # Redirect to referrer or dashboard
#          return redirect(request.referrer or url_for('dashboard'))
#     # --- End Permission Check ---
#
#     if issue.status == 'Ongoing':
#         try:
#             issue.status = 'Resolved'
#             issue.resolved_at = datetime.datetime.utcnow()
#             db.session.commit()
#             flash(f"Issue #{issue.id} marked as resolved.", "success")
#
#         except Exception as e:
#             db.session.rollback()
#             app.logger.error(f"Error resolving issue {issue_id}: {e}", exc_info=True)
#             flash("An error occurred while resolving the issue.", "error")
#     else:
#         flash(f"Issue #{issue.id} was already resolved.", "info")
#
#     return redirect(request.referrer or url_for('dashboard'))



@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not current_password or not new_password or not confirm_password:
            flash("All password fields are required.", "error")
            return redirect(request.referrer or url_for('dashboard'))

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "error")
            return redirect(request.referrer or url_for('dashboard'))

        if not g.employee.check_password(current_password):
            flash("Incorrect current password.", "error")
            return redirect(request.referrer or url_for('dashboard'))


        if len(new_password) < 5:
             flash("New password must be at least 8 characters long.", "warning")
             return redirect(request.referrer or url_for('dashboard'))

        try:
            g.employee.set_password(new_password) # This hashes the new password
            db.session.commit()
            flash("Your password has been updated successfully.", "success")
            session.clear()
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error changing password for user {g.employee.id}: {e}", exc_info=True)
            flash("An error occurred while updating your password. Please try again.", "error")

        return redirect(request.referrer or url_for('dashboard'))
    else:
        return redirect(url_for('dashboard'))




def generate_password(length=12):
    characters = string.ascii_letters + string.digits + string.punctuation
    password = ''.join(secrets.choice(characters) for i in range(length))
    return password



@app.cli.command('init-db')
def init_db_command():
    db.drop_all()
    db.create_all()
    click.echo('Initialized the database.')


@app.cli.command('create-admin')
def create_admin_command():
    """Creates the initial admin user."""
    admin_email = "greentechAdmin@gmail.com"
    admin_pass = "1234"

    if Employee.query.filter_by(email=admin_email).first():
        click.echo(f"Admin user with email '{admin_email}' already exists.")
        return

    default_greenhouse = Greenhouse.query.first()

    try:
        admin_user = Employee(
            name="Admin Officer",
            email=admin_email,
            company_id=generate_unique_company_id(),
            is_admin=True,
            available=True,
            greenhouse_id=None
        )
        admin_user.set_password(admin_pass)
        db.session.add(admin_user)
        db.session.commit()
        click.echo(f"Admin user '{admin_email}' created successfully with password '{admin_pass}'.")
        click.echo("IMPORTANT: Change this password after first login!")
    except Exception as e:
        db.session.rollback()
        click.echo(f"Error creating admin user: {e}")
