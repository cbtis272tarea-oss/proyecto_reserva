import os
import uuid
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "tu_clave_secreta_por_defecto")

# Conexión a MongoDB Atlas conservando la zona UTC
mongo_uri = os.getenv("MONGO_URI")
cliente = MongoClient(
    mongo_uri,
    serverSelectionTimeoutMS=10000,
    tz_aware=True
)

base_datos = cliente['cafeteria_dreamycoffee'] # Ajusta el nombre de tu BD si es diferente
alumnos = base_datos['reservas'] # Colección de productos
pedidos = base_datos['pedidos']

# Configuración de la colección de reservas
reservas = base_datos["reservas"]
DURACION_RESERVA_MINUTOS = 5

# Crear índices para evitar duplicados y optimizar búsquedas
reservas.create_index(
    [("reserva_id", 1), ("producto_id", 1)],
    unique=True
)
reservas.create_index("vence_en")

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================
def obtener_reserva_id():
    if not session.get("reserva_id"):
        session["reserva_id"] = uuid.uuid4().hex
    return session["reserva_id"]

def ahora_utc():
    return datetime.now(timezone.utc)

def reservar_unidad(producto_id, reserva_id):
    ahora = ahora_utc()
    vence = ahora + timedelta(minutes=DURACION_RESERVA_MINUTOS)
    oid = ObjectId(producto_id)
    
    with cliente.start_session() as mongo_session:
        with mongo_session.start_transaction():
            producto = alumnos.find_one_and_update(
                {
                    "_id": oid,
                    "estado": "Disponible",
                    "$expr": {
                        "$gt": [
                            {
                                "$subtract": [
                                    {"$ifNull": ["$existencias", 0]},
                                    {"$ifNull": ["$reservadas", 0]}
                                ]
                            },
                            0
                        ]
                    }
                },
                {"$inc": {"reservadas": 1}},
                return_document=ReturnDocument.AFTER,
                session=mongo_session
            )
            
            if producto is None:
                return False
                
            reservas.update_one(
                {"reserva_id": reserva_id, "producto_id": oid},
                {
                    "$inc": {"cantidad": 1},
                    "$set": {"estado": "activa", "vence_en": vence},
                    "$setOnInsert": {"creada_en": ahora}
                },
                upsert=True,
                session=mongo_session
            )
            return True

def liberar_reservas_vencidas():
    vencidas = list(reservas.find({
        "estado": "activa",
        "vence_en": {"$lte": ahora_utc()}
    }))
    
    for candidata in vencidas:
        with cliente.start_session() as mongo_session:
            with mongo_session.start_transaction():
                eliminada = reservas.find_one_and_delete(
                    {"_id": candidata["_id"], "estado": "activa"},
                    session=mongo_session
                )
                if eliminada:
                    alumnos.update_one(
                        {"_id": eliminada["producto_id"]},
                        {"$inc": {"reservadas": -eliminada["cantidad"]}},
                        session=mongo_session
                    )

# Context Processor para contar elementos activos en el carrito
@app.context_processor
def datos_carrito():
    reserva_id = session.get("reserva_id")
    if not reserva_id:
        return {"cantidad_carrito": 0}
    
    items = reservas.find({
        "reserva_id": reserva_id,
        "estado": "activa",
        "vence_en": {"$gt": ahora_utc()}
    })
    cantidad = sum(int(item["cantidad"]) for item in items)
    return {"cantidad_carrito": cantidad}

# ==========================================
# RUTAS DE LA APLICACIÓN (CLIENTE)
# ==========================================

@app.route("/")
def inicio():
    liberar_reservas_vencidas()
    lista = list(alumnos.find({}))
    for producto in lista:
        producto["disponibles"] = max(
            0,
            int(producto.get("existencias", 0)) - int(producto.get("reservadas", 0))
        )
    return render_template("index.html", productos=lista)

@app.route("/menu")
def menu():
    liberar_reservas_vencidas()
    lista = list(alumnos.find({}))
    for producto in lista:
        producto["disponibles"] = max(
            0,
            int(producto.get("existencias", 0)) - int(producto.get("reservadas", 0))
        )
    return render_template("menu.html", productos=lista)

@app.route("/carrito/agregar/<producto_id>", methods=["POST"])
def agregar_carrito(producto_id):
    if not ObjectId.is_valid(producto_id):
        abort(404)
        
    liberar_reservas_vencidas()
    reserva_id = obtener_reserva_id()
    
    if reservar_unidad(producto_id, reserva_id):
        flash("Producto reservado durante 5 minutos.", "success")
    else:
        flash("La última unidad acaba de ser reservada.", "warning")
        
    return redirect(request.referrer or url_for("menu"))

@app.route("/carrito")
def ver_carrito():
    liberar_reservas_vencidas()
    reserva_id = session.get("reserva_id")
    productos_carrito = []
    total = 0
    
    items = list(reservas.find({
        "reserva_id": reserva_id,
        "estado": "activa",
        "vence_en": {"$gt": ahora_utc()}
    }) if reserva_id else [])
    
    for item in items:
        producto = alumnos.find_one({"_id": item["producto_id"]})
        if not producto:
            continue
        cantidad = int(item["cantidad"])
        precio = float(producto.get("precio", 0))
        subtotal = round(precio * cantidad, 2)
        
        productos_carrito.append({
            "id": str(producto["_id"]),
            "nombre": producto.get("nombre", "Producto"),
            "precio": precio,
            "cantidad": cantidad,
            "subtotal": subtotal
        })
        total += subtotal
        
    vence_en = min((i["vence_en"] for i in items), default=None)
    
    return render_template(
        "carrito.html",
        productos=productos_carrito,
        total=round(total, 2),
        vence_en=vence_en
    )

@app.route("/carrito/eliminar/<producto_id>", methods=["POST"])
def eliminar_carrito(producto_id):
    if not ObjectId.is_valid(producto_id):
        abort(404)
        
    reserva_id = session.get("reserva_id")
    oid = ObjectId(producto_id)
    
    with cliente.start_session() as mongo_session:
        with mongo_session.start_transaction():
            eliminada = reservas.find_one_and_delete(
                {
                    "reserva_id": reserva_id,
                    "producto_id": oid,
                    "estado": "activa"
                },
                session=mongo_session
            )
            if eliminada:
                alumnos.update_one(
                    {"_id": oid},
                    {"$inc": {"reservadas": -eliminada["cantidad"]}},
                    session=mongo_session
                )
                
    flash("Reserva cancelada y producto liberado.", "success")
    return redirect(url_for("ver_carrito"))


# ==========================================
# RUTAS DE ADMINISTRACIÓN (CRUD PRODUCTOS)
# ==========================================

@app.route("/admin/productos")
def admin_productos():
    liberar_reservas_vencidas()
    lista = list(alumnos.find({}))
    for producto in lista:
        producto["disponibles"] = max(
            0,
            int(producto.get("existencias", 0)) - int(producto.get("reservadas", 0))
        )
    return render_template("admin_productos.html", productos=lista)

@app.route("/admin/productos/nuevo", methods=["GET", "POST"])
def admin_nuevo_producto():
    if request.method == "POST":
        nombre = request.form.get("nombre")
        precio = float(request.form.get("precio", 0))
        existencias = int(request.form.get("existencias", 0))
        
        alumnos.insert_one({
            "nombre": nombre,
            "precio": precio,
            "existencias": existencias,
            "reservadas": 0,
            "estado": "Disponible"
        })
        flash("Producto creado exitosamente.", "success")
        return redirect(url_for("admin_productos"))
    
    return render_template("admin_form_producto.html", accion="Nuevo", producto=None)

@app.route("/admin/productos/editar/<id>", methods=["GET", "POST"])
def admin_editar_producto(id):
    if not ObjectId.is_valid(id):
        abort(404)
        
    oid = ObjectId(id)
    producto = alumnos.find_one({"_id": oid})
    
    if not producto:
        abort(404)
        
    if request.method == "POST":
        nombre = request.form.get("nombre")
        precio = float(request.form.get("precio", 0))
        existencias = int(request.form.get("existencias", 0))
        
        alumnos.update_one(
            {"_id": oid},
            {
                "$set": {
                    "nombre": nombre,
                    "precio": precio,
                    "existencias": existencias
                }
            }
        )
        flash("Producto actualizado exitosamente.", "success")
        return redirect(url_for("admin_productos"))
        
    return render_template("admin_form_producto.html", accion="Editar", producto=producto)

@app.route("/admin/productos/eliminar/<id>", methods=["POST"])
def admin_eliminar_producto(id):
    if not ObjectId.is_valid(id):
        abort(404)
        
    alumnos.delete_one({"_id": ObjectId(id)})
    flash("Producto eliminado correctamente.", "success")
    return redirect(url_for("admin_productos"))


if __name__ == "__main__":
    app.run(debug=True)