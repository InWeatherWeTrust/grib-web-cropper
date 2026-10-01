import sys
import os
import json
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

app = Flask(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CURRENT_FILE = {
    "path": "",
    "filename": "",
    "params": []
}

# Variable globale pour suivre la progression (de 0 à 100)
PROGRESS = 0

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/upload', methods=['POST'])
def upload_grib():
    if 'file' not in request.files:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Nom de fichier vide"}), 400

    target_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(target_path)

    try:
        fin = pygrib.open(target_path)
        unique_params = set()
        for grb in fin:
            try:
                name = getattr(grb, 'name', 'Unknown Parameter')
                short_name = getattr(grb, 'shortName', str(getattr(grb, 'parameterNumber', 'unk')))
                unique_params.add(name + " (" + short_name + ")")
            except Exception:
                continue
        fin.close()

        CURRENT_FILE["path"] = target_path
        CURRENT_FILE["filename"] = file.filename
        CURRENT_FILE["params"] = sorted(list(unique_params))

        return jsonify({
            "filename": file.filename,
            "params": CURRENT_FILE["params"]
        })
    except Exception as e:
        if os.path.exists(target_path):
            os.remove(target_path)
        return jsonify({"error": "Erreur de lecture pygrib : " + str(e)}), 500

@app.route('/api/crop', methods=['POST'])
def crop_grib():
    global PROGRESS
    PROGRESS = 0  # Réinitialisation

    data = request.json
    bounds = data.get("bounds")
    selected_items = data.get("params")

    if not bounds or not selected_items or not CURRENT_FILE["path"]:
        return jsonify({"error": "Données incomplètes"}), 400

    output_filename = "crop_" + CURRENT_FILE["filename"]
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)

    try:
        # Étape 1 : Compter le nombre total de messages pour calculer le pourcentage
        fin = pygrib.open(CURRENT_FILE["path"])
        total_messages = sum(1 for _ in fin)
        fin.close()

        if total_messages == 0:
            total_messages = 1

        # Étape 2 : Lancer la découpe réelle
        fin = pygrib.open(CURRENT_FILE["path"])
        fout = open(output_path, "wb")

        n = bounds["n"]
        s = bounds["s"]
        e = bounds["e"]
        w = bounds["w"]

        count = 0
        for index, grb in enumerate(fin):
            try:
                name = getattr(grb, 'name', 'Unknown Parameter')
                short_name = getattr(grb, 'shortName', str(getattr(grb, 'parameterNumber', 'unk')))
                param_str = name + " (" + short_name + ")"
            except Exception:
                continue

            if param_str in selected_items:
                try:
                    sub_data, lats, lons = grb.data(lat1=s, lat2=n, lon1=w, lon2=e)
                    grb.values = sub_data
                    fout.write(grb.tostring())
                    count += 1
                except Exception:
                    fout.write(grb.tostring())
                    count += 1

            # Mise à jour de la progression globale
            PROGRESS = int(((index + 1) / total_messages) * 100)

        fin.close()
        fout.close()

        PROGRESS = 100  # Fin de l'opération
        return jsonify({
            "success": True,
            "message": str(count) + " couches exportées",
            "download_url": "/api/download"
        })
    except Exception as e:
        PROGRESS = 0
        return jsonify({"error": "Erreur de découpe : " + str(e)}), 500

@app.route('/api/download')
def download_file():
    output_filename = "crop_" + CURRENT_FILE["filename"]
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True, download_name=output_filename)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
