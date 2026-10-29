from flask import Flask, render_template
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from app_web import app as cep_app

app = Flask(__name__)


@app.route('/')
def home():
    return render_template('home.html')


# Seul le module CEP/IFS est actif. app_arpege.py n'est plus importé.
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/cep': cep_app})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
