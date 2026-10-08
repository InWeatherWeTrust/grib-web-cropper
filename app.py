import os
from flask import Flask, render_template
from app_web import app as cep_app
from app_arpege import app as arpege_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

app = Flask(__name__)

@app.route('/')
def home():
    # Page d'accueil du Hub Central
    return render_template('home.html')

# Liaison des deux applications distinctes
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
    '/cep': cep_app,
    '/arpege': arpege_app
})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
