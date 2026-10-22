from flask import Flask
from flask_basicauth import BasicAuth

app = Flask(__name__)

app.config['BASIC_AUTH_USERNAME'] = 'ddagent-user'
app.config['BASIC_AUTH_PASSWORD'] = 'passw0rd'

basic_auth = BasicAuth(app)


@app.route('/')
@basic_auth.required
def serve_metrics():
    with open("metrics.txt", "r") as f:
        metrics = f.read()
    return metrics


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
