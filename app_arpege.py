import sys
import os
import json
import shutil
import urllib.request
import ssl
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
    """Calcule l'URL brute exacte sur le serveur de stockage IP fixe de l'ECMWF"""
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
    """SCAN DYNAMIQUE RÉEL : Analyse les premiers Mo du vrai fichier pour lister les paramètres présents"""
    data = request.json
    selected_steps = data.get("steps",)
    levtype = data.get("levtype", "sfc")
    
    if not selected_steps:
        return jsonify({"error": "Cochez au moins une échéance pour le scan."}), 400
        
    first_step = sorted([int(s) for s in selected_steps])
    url = get_ecmwf_arpege_url(first_step, levtype)
    
    scan_path = os.path.join(UPLOAD_FOLDER, f"real_scan_{first_step}_{levtype}.grib2")
    if os.path.exists(scan_path): os.remove(scan_path)
    
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url)
        req.add_header('Host', 'data.ecmwf.int')
        req.add_header('User-Agent', 'Mozilla/5.0')
        # SÉCURITÉ RAM : On demande uniquement les 35 premiers mégaoctets du flux pour lire les en-têtes
        req.add_header('Range', 'bytes=0-36700160')
        req.add_header('Connection', 'close')
        
        with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
            with open(scan_path, "wb") as f:
                f.write(response.read())
                
        # Ouverture et extraction par pygrib des vrais identifiants contenus dans le fichier GRIB2
        fin = pygrib.open(scan_path)
        params_set = set()
        levels_set = set()
        
        for grb in fin:
            try:
                name = getattr(grb, 'name', 'Unknown')
                short_name = getattr(grb, 'shortName', '')
                level = getattr(grb, 'level', 0)
                lev_type = getattr(grb, 'typeOfLevel', 'surface')
                
                if short_name:
                    params_set.add(f"{name} ({short_name})")
                if level is not None:
                    if "surface" in str(lev_type).lower():
                        levels_set.add("0 (surface)")
                    else:
                        levels_set.add(f"{level} hPa")
            except Exception:
                continue
        fin.close()
        
        if os.path.exists(scan_path): os.remove(scan_path)
        
        if len(params_set) == 0:
            return jsonify({"error": "Le fragment d'en-tête ne contient aucun message lisible. Réessayez."}), 400
            
        return jsonify({
            "success": True,
            "params": sorted(list(params_set)),
            "levels": sorted(list(levels_set), key=lambda x: int(x.split()[0]) if x.split() and x.split()[0].isdigit() else 0)
        })
    except Exception as e:
        if os.path.exists(scan_path): os.remove(scan_path)
        return jsonify({"error": f"Impossible de lire l'entête réelle du fichier : {str(e)}"}), 500

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
    
    # Extraction propre du code court (ex: "u", "v", "gh") choisi dynamiquement
    selected_params = []
    for p in selected_params_raw:
        if "(" in p:
            selected_params.append(p.split("(")[-1].replace(")", "").strip())
        else:
            selected_params.append(p.strip())
            
    selected_levels = []
    for l in selected_levels_raw:
        try:
            parts = str(l).strip().split()
            if parts and parts[0].isdigit():
                selected_levels.append(int(parts[0]))
            elif "surface" in str(l).lower():
                selected_levels.append(0)
        except:
            continue
        
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
                
                try:
                    req = urllib.request.Request(url)
                    req.add_header('Host', 'data.ecmwf.int')
                    req.add_header('User-Agent', 'Mozilla/5.0')
                    req.add_header('Connection', 'close')
                    
                    with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                except Exception as step_error:
                    print(f"Saut de l'échéance {step_val} : {step_error}")
                    continue
                            
                if os.path.exists(chunk_raw_path) and os.path.getsize(chunk_raw_path) > 0:
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            short_name = getattr(grb, 'shortName', '')
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            
                            # Correspondance dynamique stricte
                            if short_name in selected_params and level in selected_levels:
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    os.remove(chunk_raw_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite. Vos critères ne correspondent à aucun message binaire."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur d'extraction binaire : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
