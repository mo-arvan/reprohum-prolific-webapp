# This is the webapp for hosting Prolific surveys for the Edinburgh Napier University lab within the reprohum project.
# The data is in csv format, containing the data from the survey. File name should be DATA variable
# The user interface is the interface.html file, which is a template for the survey.
# Each interface has the following structure ${outputb1} where inside the brackets is the name of the variable.
# There can be multiple variables - which should be defined in the python code to match the variable names
# in the csv file.
import datetime
import json
import logging
import os
from threading import Lock
import csv
import pandas as pd
from flask import Flask, request, render_template_string

import CreateDatabase as create_db
import DataManager as dm

# Should probably note the 1hr limit on the interface/instructions.
# NOTE: If you do not want to expire tasks, set this to a very large number, 0 will not work.
# Maximum time in seconds that a task can be assigned to a participant before it is abandoned - 1 hour = 3600 seconds
MAX_TIME = int(os.environ.get("MAX_TIME", 3600))
TEMPLATE = os.environ.get("TEMPLATE", "templates/example.html")
DATA = os.environ.get("DATA", "example.csv")
PORT = os.environ.get("FLASK_SERVER_PORT", 9090)
NUMBER_OF_TASKS = int(os.environ.get("NUMBER_OF_TASKS", 60))
COMPLETIONS_PER_TASK = int(os.environ.get("COMPLETIONS_PER_TASK", 3))
DATABASE_FILE = os.environ.get("DATABASE_FILE", "tasks.db")
RESULTS_DIR = os.environ.get("RESULTS_DIR", "data")
LOG_DIR = os.environ.get("LOG_DIR", "study/logs")
COMPLETION_CODE = os.environ.get("COMPLETION_CODE", "1234")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

server_start_time_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

log_file = os.path.join(LOG_DIR, f"{server_start_time_str}.log")
logging.basicConfig(filename=log_file,
                    filemode='a',
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)

logger = logging.getLogger('reprohum-server')

logger.info("Server started.")

if not os.path.exists(DATABASE_FILE):
    create_db.initDatabase(DATABASE_FILE)
    create_db.initTasks(NUMBER_OF_TASKS, COMPLETIONS_PER_TASK, DATABASE_FILE)

# Load the data from the csv file into a pandas dataframe
df = pd.read_csv(DATA)
# Create a list of the column names in the csv file
column_names = df.columns.values.tolist()

mutex = Lock()
# Create a dictionary of tasks, where each task is a row in the csv file
# This should probably be saved to a file rather than in memory, so that we can restart the server and not lose data.
# tasks = {i: {"completed_count": 0, "participants": [], "assigned_times": []} for i in range(len(df))}

# TODO: Might require adding CORS headers to allow requests. See https://flask-cors.readthedocs.io/en/latest/
app = Flask(__name__,
            static_url_path='',
            static_folder='static',
            template_folder='templates')  # Create the flask app


# MTurk template replacement tokens is different to Jinja2, so we just do it manually here.
def preprocess_html(html_content, df, task_id=-1):
    for column_name in column_names:
        html_content = html_content.replace("${" + column_name + "}", str(df[column_name].values[0]))

    html_content = html_content.replace("${task_id}", str(task_id))

    return html_content


# Routes

# This is the index route, which will just say nothing here. If it gets a POST request, it will save the HIT response JSON to a file.
# NOTE: The HTML interfaces should be updated with a button that compiles the answers as a JSON object and POSTS to this app.
# TODO: Validate submission is JSON and has required fields (task_id, prolific_pid, etc) else could crash
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Print JSON from POST request
        logger.info(f"Received JSON: {request.json}")

        must_have_keys = ['task_id', 'prolific_pid', 'session_id', 'study_id']
        for i in range(32):
            must_have_keys.append(f'meaning{i}')

        for key in must_have_keys:
            if key not in request.json:
                return {"result": f"Invalid Response, try again."}, 400

        # Save JSON to file with task_id as the folder and uuid as the filename
        task_id = request.json['task_id']
        prolific_pid = request.json['prolific_pid']
        folder_path = os.path.join(RESULTS_DIR, str(task_id))
        os.makedirs(folder_path, exist_ok=True)  # Create folder if it doesn't exist

        file_path = os.path.join(folder_path, f"{task_id}.json")

        with open(file_path, 'w') as outfile:
            json.dump(request.json, outfile)

        # Complete the task
        logger.info(task_id, request.json, prolific_pid)

        complete = dm.complete_task(task_id, str(request.json), prolific_pid, DATABASE_FILE)

        if complete == -1:
            return {"result": "Something went wrong? Is this your task?"}, 500

        response_json = {"result": "OK", "completion_code": COMPLETION_CODE}
        logger.info(f"Returning JSON: {response_json}")
        return response_json, 200
    else:
        return "Nothing Here.", 200


# This route is used for testing the interface on specific rows of the csv file
@app.route('/row/<int:row_id>', methods=['GET', 'POST'])
def row(row_id):
    # Read the HTML template file
    with open(f'templates/{TEMPLATE}', 'r') as html_file:
        html_content = html_file.read()
    # Preprocess the HTML content
    processed_html = preprocess_html(html_content, df.iloc[[row_id]], row_id)
    return render_template_string(processed_html)


# Study route, get PROLIFIC_PID, STUDY_ID and SESSION_ID from URL parameters
# This route will assign a task to a participant and return the HTML interface for that task
# It *should* be updated to allow the participant to continue where they left off if they refresh the page - this will be implemented using the PROLIFIC_PID searching on pending tasks.
# TODO: Some kind of handling for case where there are no tasks left for that worker - i.e there are tasks left but none that they haven't already completed
@app.route('/study/')
def study():
    # Get PROLIFIC_PID, STUDY_ID and SESSION_ID from URL parameters
    prolific_pid = request.args.get('PROLIFIC_PID')
    session_id = request.args.get('SESSION_ID')
    study_id = request.args.get('STUDY_ID')

    # Read the HTML template file
    with open(TEMPLATE, 'r') as html_file:
        html_content = html_file.read()

    # Allocate task or find already allocated task
    if prolific_pid is None or session_id is None:
        return "PROLIFIC_PID and SESSION_ID are required parameters.", 400
    else:
        # we will make the entire task allocation process atomic to avoid potential concurrency issues
        # should not introduce any side effects
        with mutex:
            task_id, task_number = dm.allocate_task(prolific_pid, session_id, DATABASE_FILE)

    # If there is a database error, return the error message and status code
    if task_id == "Database Error - Please try again, if the problem persists contact us." and task_number == -1:
        return task_id, 500

    # If no task is available, return a message
    if task_id is None:
        return "No tasks available", 400

    html_content = preprocess_html(html_content, df.iloc[[task_number]],
                                   task_id)  # Params: html_content, df, task_id=-1
    html_content += f'<input type="hidden" id="prolific_pid" name="prolific_pid" value="{prolific_pid}">\n'
    html_content += f'<input type="hidden" id="session_id" name="session_id" value="{session_id}">\n'
    html_content += f'<input type="hidden" id="study_id" name="study_id" value="{session_id}">\n'
    html_content += f'<input type="hidden" id="task_id" name="task_id" value="{task_id}">\n'
    return render_template_string(html_content)


# This route is used for testing - it will return the tasks dictionary showing the number of participants assigned to each task
@app.route('/tasksallocated')
def aloced():
    tasks = dm.get_all_tasks(DATABASE_FILE)

    return tasks


# Show a specific task_id's result in the database
@app.route('/results/<task_id>')
def results(task_id):
    result = dm.get_specific_result(str(task_id), DATABASE_FILE)
    if result is None:
        return "No result found for task_id: " + str(task_id), 400
    return str(result)


# Check for tasks that have been assigned for more than 1 hour and then open them up again for participants to complete. Code at bottom of file will make this run every hour.
# The route is set up for testing to manually check for abandoned tasks. It will return a list of abandoned tasks that have been opened up again.
@app.route('/abdn')
def check_abandonment():
    logger.info("Checking for abandoned tasks...")
    dm.expire_tasks(DATABASE_FILE, MAX_TIME)  # Do not update MAX_TIME manually, use MAX_TIME variable

    tasks = dm.get_all_tasks(DATABASE_FILE)

    return tasks, 200


@app.route('/export')
def export():
    if request.method != 'GET':
        return "Invalid request", 400

    tasks_df = df
    task_db_list = dm.get_all_tasks(DATABASE_FILE)
    results_db_list = dm.get_all_results(DATABASE_FILE)

    if task_db_list is None or results_db_list is None:
        return "No tasks or results found", 400

    task_db_columns = ['id', 'task_number', 'prolific_id', 'time_allocated', 'session_id', 'status']
    task_db_df = pd.DataFrame(task_db_list, columns=task_db_columns)

    results_db_columns = ['id', 'json_string', 'prolific_id']
    results_db_df = pd.DataFrame(results_db_list, columns=results_db_columns)

    task_results_df = pd.merge(task_db_df, results_db_df, on=['id', "prolific_id"], how='left')

    tasks_df['task_number'] = tasks_df.index + 1

    tasks_joined = pd.merge(tasks_df, task_results_df, on='task_number', how='inner')

    file_path = os.path.join(RESULTS_DIR, "tasks_joined")
    tasks_joined.to_csv(file_path + '.csv', index=False, quoting=csv.QUOTE_ALL)
    tasks_joined.to_pickle(file_path + '.pkl')

    return "Exported tasks_joined.csv and tasks_joined.pkl", 200


# Scheduler

# Run the check_abandonment function every hour - this has to be before the app.run() call
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_abandonment, trigger="interval",
                  seconds=MAX_TIME)  # Do not update seconds manually, use MAX_TIME
scheduler.start()

# CLI Entry Point (for testing) - python main.py

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=True)
