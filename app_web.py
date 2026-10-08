import os
import json
from flask import Flask, render_template, request, jsonify, send_file
from ecmwf.opendata import Client
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

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 10
    
    data = request.json
    bounds = data.get("bounds")
    selected_params = data.get("params")
    selected_levels = data.get("levels")
    selected_steps = data.get("steps")  # Récupère la liste des échéances (ex:)
    
    # Conversion en entiers pour l'API ECMWF
    steps_list = [int(s) for s in selected_steps] if selected_steps else [12]
    
    client = Client()
    raw_filename = "ecmwf_global.grib2"
    raw_path = os.path.join(UPLOAD_FOLDER, raw_filename)
    output_filename = "crop_ecmwf.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    
    try:
        PROGRESS = 30
        
        # L'API télécharge à la carte les champs, niveaux ET étapes demandés
        client.retrieve(
            date=-1,
            time="00",
            step=steps_list,  # Injection de la liste des échéances choisies
            stream="oper",
            type="fc",
            levtype="pl",
            levelist=selected_levels,
            param=selected_params,
            target=raw_path
        )
        
        PROGRESS = 60
        
        fin = pygrib.open(raw_path)
        fout = open(output_path, "wb")
        total_msg = sum(1 for _ in fin)
        fin.seek(0)
        
        for index, grb in enumerate(fin):
            try:
                sub_data, lats, lons = grb.data(
                    lat1=bounds["s"], lat2=bounds["n"], 
                    lon1=bounds["w"], lon2=bounds["e"]
                )
                grb.values = sub_data
                fout.write(grb.tostring())
            except Exception:
                fout.write(grb.tostring())
                
            PROGRESS = 60 + int(((index + 1) / total_msg) * 40)
            
        fin.close()
        fout.close()
        
        if os.path.exists(raw_path):
            os.remove(raw_path)
            
        PROGRESS = 100
        return jsonify({"success": True, "download_url": f"/api/download/{output_filename}"})
        
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(raw_path): os.remove(raw_path)
        return jsonify({"error": f"Erreur ECMWF : {str(e)}"}), 500


@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)
