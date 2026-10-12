import sys
import os
import json
import shutil
import urllib.request
import ssl
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file

base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(base_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

def resolve_dns_google_https(domain):
    """Bypasse le DNS défaillant de Render en résolvant l'IP de Météo-France via Google HTTPS"""
    try:
        ctx = ssl._create_unverified_context()
        dns_url = f"https://dns.google{domain}&type=A"
        req = urllib.request.Request(dns_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if "Answer" in data and len(data["Answer"]) > 0:
                return data["Answer"]["data"]
    except Exception as e:
        print(f"⚠️ Échec Résolution Google DNS pour {domain}: {e}")
    return None

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
    """Génère instantanément les listes de paramètres réels selon le mode sélectionné"""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    if levtype == "pl":
        # Liste complète de vos altitudes de routage validées
        available_params = [
            "U-component of wind (u)",
            "V-component of wind (v)",
            "Geopotential height (gh)",
            "Temperature (t)"
        ]
        available_levels = ["700 hPa", "500 hPa", "400 hPa", "300 hPa", "250 hPa"]
    else:
        available_params = [
            "10m u-component of wind (10u)",
            "10m v-component of wind (10v)",
            "Mean sea level pressure (msl)",
            "2m temperature (2t)",
            "Total precipitation (tp)"
        ]
        available_levels = ["0 (surface)"]
    
    return jsonify({
        "success": True,
        "params": sorted(available_params),
        "levels": available_levels
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
    
    # Nettoyage des chaînes courtes (ex: '10u', 'u', 'msl')
    selected_params = [p.split("(")[-1].replace(")", "").strip() for p in selected_params_raw if "(" in p]
    selected_levels = [int(l.split()[0]) for l in selected_levels_raw if l.split()[0].isdigit()]
    
    yesterday = datetime.utcnow() - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    domain = "public-api.meteofrance.fr"
    dynamic_ip = resolve_dns_google_https(domain)
    
    total_steps = len(selected_steps)
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                # Formatage temporel ISO exigé par Météo-France
                time_coverage = f"{date_str}T00:00:00Z/PT{step_val}H"
                
                for param in selected_params:
                    # Cartographie des variables d'altitude et de surface vers l'API Météo-France
                    if param == '10u' or param == 'u':
                        coverage_id = 'U_COMPONENT_OF_WIND__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND' if levtype == 'sfc' else 'U_COMPONENT_OF_WIND__ISOBARIC_SURFACE'
                    elif param == '10v' or param == 'v':
                        coverage_id = 'V_COMPONENT_OF_WIND__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND' if levtype == 'sfc' else 'V_COMPONENT_OF_WIND__ISOBARIC_SURFACE'
                    elif param == 'msl':
                        coverage_id = 'PRESSURE__SEA_LEVEL'
                    elif param == '2t' or param == 't':
                        coverage_id = 'TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND' if levtype == 'sfc' else 'TEMPERATURE__ISOBARIC_SURFACE'
                    elif param == 'gh':
                        coverage_id = 'GEOPOTENTIAL__ISOBARIC_SURFACE'
                    elif param == 'tp':
                        coverage_id = 'TOTAL_PRECIPITATION__SURFACE'
                    else:
                        continue
                        
                    # Construction des arguments de découpe par l'API WCS de Météo-France
                    query_string = (
                        f"/public/DPOpenData/wcs/v1/ARPEGE/025/GLOB?"
                        f"SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGE_ID={coverage_id}&"
                        f"SUBSET=long({bounds['w']},{bounds['e']})&SUBSET=lat({bounds['s']},{bounds['n']})&"
                        f"TIME={time_coverage}&FORMAT=GRIB2"
                    )
                    
                    # On y injecte le niveau d'altitude isobarique hPa si nous sommes en mode PL
                    if levtype == "pl":
                        for lvl in selected_levels:
                            url = f"https://{dynamic_ip if dynamic_ip else domain}{query_string}&SUBSET=std_level({lvl})"
                            req = urllib.request.Request(url)
                            if dynamic_ip: req.add_header('Host', domain)
                            req.add_header('User-Agent', 'Mozilla/5.0')
                            req.add_header('Connection', 'close')
                            try:
                                with urllib.request.urlopen(req, context=ctx, timeout=20) as response:
                                    fout.write(response.read())
                            except: continue
                    else:
                        url = f"https://{dynamic_ip if dynamic_ip else domain}{query_string}"
                        req = urllib.request.Request(url)
                        if dynamic_ip: req.add_header('Host', domain)
                        req.add_header('User-Agent', 'Mozilla/5.0')
                        req.add_header('Connection', 'close')
                        try:
                            with urllib.request.urlopen(req, context=ctx, timeout=20) as response:
                                fout.write(response.read())
                        except: continue
                        
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Météo-France n'a retourné aucun message GRIB pour cette zone ou cette date."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur d'extraction WCS : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
