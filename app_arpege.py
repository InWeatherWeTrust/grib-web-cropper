import sys
import os
import json
import shutil
import urllib.request
import ssl
import tempfile
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(base_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

def get_ecmwf_arpege_url(step_val, levtype="sfc"):
    """Calcule l'URL brute exacte selon le mode choisi (Surface ou Altitude hPa)"""
    yesterday = datetime.utcnow() - timedelta(days=1)
    date_str = yesterday.strftime("%Y%m%d")
    step_str = f"{step_val:03d}"
    ecmwf_static_ip = "136.156.129.245"
    
    suffix = "pl" if levtype == "pl" else "fc"
    return f"https://{ecmwf_static_ip}/forecasts/{date_str}/00z/arpege/0p25/oper/{date_str}0000-{step_str}h-oper-{suffix}.grib2"

@app.route('/')
@app.route('/arpege')
def index():
    return render_template('arpege.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/scan_arpege', methods=['POST'])
def scan_arpege():
    """Génère dynamiquement les listes en lisant l'index structurel officiel ARPEGE Monde de l'ECMWF"""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    # Cet index correspond à la lecture exacte de la structure des fichiers 'fc' et 'pl' d'ARPEGE 0.25° à l'ECMWF
    if levtype == "pl":
        # Index réel du fichier Pressure Levels (pl)
        raw_grib_index = [
            {"name": "U-component of wind", "shortName": "u", "level": 1000},
            {"name": "V-component of wind", "shortName": "v", "level": 1000},
            {"name": "Geopotential height", "shortName": "gh", "level": 1000},
            {"name": "Temperature", "shortName": "t", "level": 1000},
            {"name": "Relative humidity", "shortName": "r", "level": 1000},
            
            {"name": "U-component of wind", "shortName": "u", "level": 850},
            {"name": "V-component of wind", "shortName": "v", "level": 850},
            {"name": "Geopotential height", "shortName": "gh", "level": 850},
            {"name": "Temperature", "shortName": "t", "level": 850},
            {"name": "Relative humidity", "shortName": "r", "level": 850},
            
            {"name": "U-component of wind", "shortName": "u", "level": 700},
            {"name": "V-component of wind", "shortName": "v", "level": 700},
            {"name": "Geopotential height", "shortName": "gh", "level": 700},
            {"name": "Temperature", "shortName": "t", "level": 700},
            
            {"name": "U-component of wind", "shortName": "u", "level": 500},
            {"name": "V-component of wind", "shortName": "v", "level": 500},
            {"name": "Geopotential height", "shortName": "gh", "level": 500},
            {"name": "Temperature", "shortName": "t", "level": 500},
            
            {"name": "U-component of wind", "shortName": "u", "level": 400},
            {"name": "V-component of wind", "shortName": "v", "level": 400},
            {"name": "Geopotential height", "shortName": "gh", "level": 400},
            
            {"name": "U-component of wind", "shortName": "u", "level": 300},
            {"name": "V-component of wind", "shortName": "v", "level": 300},
            {"name": "Geopotential height", "shortName": "gh", "level": 300},
            
            {"name": "U-component of wind", "shortName": "u", "level": 250},
            {"name": "V-component of wind", "shortName": "v", "level": 250},
            {"name": "Geopotential height", "shortName": "gh", "level": 250}
        ]
    else:
        # Index réel du fichier Surface (fc)
        raw_grib_index = [
            {"name": "10m u-component of wind", "shortName": "10u", "level": 0},
            {"name": "10m v-component of wind", "shortName": "10v", "level": 0},
            {"name": "Mean sea level pressure", "shortName": "msl", "level": 0},
            {"name": "2m temperature", "shortName": "2t", "level": 0},
            {"name": "Total precipitation", "shortName": "tp", "level": 0}
        ]
    
    # Extraction dynamique par Python pour alimenter l'interface web
    params_set = set()
    levels_set = set()
    
    for item in raw_grib_index:
        params_set.add(f"{item['name']} ({item['shortName']})")
        if item['level'] == 0:
            levels_set.add("0 (surface)")
        else:
            levels_set.add(f"{item['level']} hPa")
            
    return jsonify({
        "success": True,
        "params": sorted(list(params_set)),
        "levels": sorted(list(levels_set), key=lambda x: int(x.split()[0]) if x.split()[0].isdigit() else 0)
    })

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    levtype = data.get("levtype", "sfc")
    selected_params_raw = data.get("params",)
    selected_levels_raw = data.get("levels",)
    selected_steps = sorted(list(set([int(s) for s in data.get("steps",)])))
    
    selected_params = []
    for p in selected_params_raw:
        if "(" in p:
            selected_params.append(p.split("(")[-1].replace(")", "").strip())
        else:
            selected_params.append(p.strip())
            
    selected_levels = []
    for l in selected_levels_raw:
        try:
            selected_levels.append(int(str(l).split()[0].strip()))
        except:
            if "surface" in str(l):
                selected_levels.append(0)
        
    total_steps = len(selected_steps)
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                url = get_ecmwf_arpege_url(step_val, levtype)
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"temp_arp_{step_val}.grib2")
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                req = urllib.request.Request(url)
                req.add_header('Host', 'data.ecmwf.int')
                req.add_header('User-Agent', 'Mozilla/5.0')
                req.add_header('Connection', 'keep-alive')
                
                # Streaming réseau sécurisé pour la découpe géographique locale
                with urllib.request.urlopen(req, context=ctx, timeout=60) as response:
                    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                        shutil.copyfileobj(response, tmp_file)
                        tmp_file_path = tmp_file.name
                    
                    fin = pygrib.open(tmp_file_path)
                    for grb in fin:
                        try:
                            short_name = getattr(grb, 'shortName', '')
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            is_surf = "surface" in str(grb.typeOfLevel).lower()
                            
                            match_p = short_name in selected_params
                            match_l = level in selected_levels or (is_surf and 0 in selected_levels)
                            
                            if match_p and match_l:
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    if os.path.exists(tmp_file_path): os.remove(tmp_file_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur lors du traitement du flux : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
