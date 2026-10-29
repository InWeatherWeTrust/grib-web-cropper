from flask import Flask, render_template
from app_web import app as cep_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('home.html')   # CEP actif, autres modèles « bientôt »

app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/cep': cep_app})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
