import sys
import os
import json
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

app = Flask(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/crop_fast', methods=['POST'])
def crop_fast_grib():
    global PROGRESS
    PROGRESS = 0
    
    if 'file' not in request.files:
        return jsonify({"error": "Fichier manquant"}), 400
        
    file = request.files['file']
    bounds = json.loads(request.form.get("bounds", "{}"))
    selected_items = json.loads(request.form.get("params", "[]"))
    
    # Étape 1 : Sauvegarde temporaire du fichier brut
    temp_path = os.path.join(UPLOAD_FOLDER, "raw_" + file.filename)
    file.save(temp_path)
    
    output_filename = "crop_" + file.filename
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    try:
        fin = pygrib.open(temp_path)
        total_messages = sum(1 for _ in fin)
        fin.close()
        
        if total_messages == 0: total_messages = 1

        fin = pygrib.open(temp_path)
        fout = open(output_path, "wb")
        
        count = 0
        for index, grb in enumerate(fin):
            try:
                name = getattr(grb, 'name', 'Unknown Parameter')
                short_name = getattr(grb, 'shortName', str(getattr(grb, 'parameterNumber', 'unk')))
                param_str = name + " (" + short_name + ")"
            except Exception:
                continue
                
            # Vérification de découpe
            if param_str in selected_items or len(selected_items) == 0:
                try:
                    sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                    grb.values = sub_data
                    fout.write(grb.tostring())
                    count += 1
                except Exception:
                    fout.write(grb.tostring())
                    count += 1
            
            PROGRESS = int(((index + 1) / total_messages) * 100)
            
        fin.close()
        fout.close()
        
        # Nettoyage immédiat du fichier de 130 Mo pour libérer la RAM de Render
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
        PROGRESS = 100
        return jsonify({"success": True, "download_url": "/api/download/" + output_filename})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(temp_path): os.remove(temp_path)
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        # Renvoie le fichier découpé et le supprime après envoi pour garder le serveur propre
        response = send_file(output_path, as_attachment=True)
        return response
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
