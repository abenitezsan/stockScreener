# Valoración y precios de compra

Referencia de cómo calcula la aplicación los precios justos, el precio de compra y el semáforo.
Los parámetros configurables están en `app/config.py` y se pueden cambiar en `.env`.

## Parámetros

| Parámetro | Valor por defecto | Uso |
|---|---|---|
| `VALUATION_WINDOW_YEARS` | 5 | Ventana de cálculo de la banda de yield y del PER medio (también se muestra 10 años) |
| `MARGIN_OF_SAFETY` | 10 % | Descuento sobre el precio justo de consenso (ajustable por valor en la lista de seguimiento) |
| `TARGET_TOTAL_RETURN` | 5 % | Rentabilidad total anual objetivo (dividendo + crecimiento), ajustable por valor |
| `GORDON_DISCOUNT_RATE` | 8 % | Tasa de descuento del modelo de Gordon |
| `GROWTH_CAP` | 5 % | Crecimiento máximo del dividendo que se admite en las proyecciones (métodos 4 y 5) |

## Datos de partida

- **Dividendos ordinarios.** Se excluyen los dividendos extraordinarios: un pago es extraordinario si
  supera el doble del mayor pago de los 400 días anteriores.
- **Dividendo TTM (D).** Suma de los dividendos ordinarios con fecha ex-dividendo en los últimos 365 días.
- **Dividendo anual.** Suma de los dividendos ordinarios por año natural. No se cuentan el año en curso
  ni el primer año del histórico si está incompleto.
- **Crecimiento del dividendo a 5 y 10 años.** Tasa de crecimiento anual compuesto (CAGR) entre el
  dividendo anual del último año completo y el de 5 o 10 años antes.
- **Años sin recorte.** Años consecutivos, contando hacia atrás, en los que el dividendo anual no baja
  más de un 1 %. **Años subiendo:** lo mismo, pero con subida estricta.
- **Dividendo estimado a 12 meses (D₁)** = D × (1 + g), con g = crecimiento a 5 años acotado entre 0 y
  `GROWTH_CAP`. Yahoo no da estimaciones de dividendos: es una estimación propia.
- **Yield histórica.** Para cada semana: dividendo TTM en esa fecha ÷ cierre semanal. Sobre la
  ventana se calcula la media y el percentil 80.

Todos los importes se expresan en la divisa de cotización (las acciones de Londres se pasan de
peniques a libras). Si la yield calculada difiere de la de Yahoo en torno a ×100, se corrige la
unidad de los dividendos y el valor queda marcado.

## Métodos de precio justo

| # | Método | Fórmula | Cuándo se aplica |
|---|---|---|---|
| 1 | Banda de yield (Weiss) | D ÷ yield media de la ventana | Al menos 3 años de dividendos |
| 2 | PER histórico | BPA estimado (si no hay, BPA de los últimos 12 meses) × PER medio | BPA > 0, al menos 3 años de PER; no se aplica a REITs |
| 3 | Chowder | D ÷ (umbral − crecimiento a 5 años) | Umbral: 12 %; 15 % si la yield < 3 %; 8 % en utilities con yield ≥ 4 %. Solo si umbral − crecimiento ≥ 2 % |
| 5 | Gordon | D₁ ÷ (r − g) | Solo sectores estables: utilities, telecos, consumo básico y REITs |

Además, el método 1 da una **referencia de compra**: D ÷ percentil 80 de la yield.

### Método 4: rentabilidad total objetivo

- Rentabilidad total esperada = yield estimada (D₁ ÷ precio) + g.
- **Precio máximo** = D₁ ÷ (objetivo − g). Es el precio por encima del cual no se espera alcanzar el
  objetivo.
- Si g ≥ objetivo, el crecimiento por sí solo ya cubre el objetivo y no hay precio máximo.
- No entra en la mediana: actúa como techo del precio de compra.

## Precio de compra y semáforo

1. **Precio justo** = mediana de los métodos 1, 2, 3 y 5 que se puedan aplicar.
2. **Precio de compra** = mín(precio justo × (1 − margen de seguridad), precio máximo del método 4).
3. Semáforo:
   - 🟢 Verde: precio ≤ precio de compra **y** el dividendo pasa el filtro de sostenibilidad.
   - 🟡 Amarillo: precio ≤ precio justo, o está en zona de compra pero falla la sostenibilidad.
   - 🔴 Rojo: precio > precio justo.
   - ⚪ Sin datos: ningún método aplicable.

## Sostenibilidad del dividendo por sector

- **Payout sobre beneficios:** `payoutRatio` de Yahoo (últimos 12 meses).
- **Payout sobre FCF:** dividendos pagados ÷ flujo de caja libre (FCF) del último ejercicio.
- **Payout sobre flujo operativo:** dividendos pagados ÷ flujo operativo del último ejercicio.

| Grupo | Sectores de Yahoo | Condición |
|---|---|---|
| General | Resto | Payout sobre beneficios ≤ 70 % y payout sobre FCF ≤ 70 % |
| Utilities y telecos | Utilities; Communication Services / Telecom Services | Payout sobre beneficios ≤ 80 % y payout sobre flujo operativo ≤ 80 % |
| REITs | Real Estate / REIT-* | Dividendos ÷ flujo operativo ≤ 90 % (aproximación a AFFO) |
| Financieras | Financial Services | Payout sobre beneficios ≤ 60 % |
| Cíclicas | Energy, Basic Materials | Payout sobre beneficios ≤ 80 % y payout sobre FCF ≤ 80 % |

Si falta un dato, esa condición no se evalúa y el valor queda marcado. Los avisos que no impiden el
verde son:
- Recorte del dividendo en los últimos 5 años.
- Yield actual superior a 1,5 veces su media (posible trampa de yield).
- Dividendos reescalados por un problema de unidades.

## Limitaciones conocidas

- Yahoo solo ofrece unos 4 ejercicios de cuentas anuales, así que el "PER medio" cubre como mucho
  esos años.
- Las cuentas se convierten a la divisa de cotización con el tipo de cambio actual, no con el
  histórico.
- El histórico de precios se guarda con frecuencia semanal (10 años) para mantener la base de datos
  ligera.
