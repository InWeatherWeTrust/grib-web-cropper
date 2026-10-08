import os
from flask import Flask, render_template
# On importe les objets "app" de vos deux serveurs séparés
from app_web import app as cep_app
from app_arpege import app as arpege_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

app = Flask(__name__)

@app.route('/')
def home():
    # Affiche la page d'accueil avec les deux gros boutons
    return render_template('home.html')

# Fusion magique et étanche des deux applications sur le même serveur Render
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
    '/cep': cep_app,       # Tout ce qui commence par /cep ira vers app_web.py
    '/arpege': arpege_app  # Tout ce qui commence par /arpege ira vers app_arpege.py
})

if __name__ == '__main__':
    # Configuration locale pour vos tests
    run_simple('localhost', 5000, app, use_reloader=True, use_debugger=True)
