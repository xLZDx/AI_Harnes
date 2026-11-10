def bmr_mifflin(sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    """Basal metabolic rate, Mifflin-St Jeor."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base - 161 if sex == "male" else base + 5


ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "high": 1.725}


def daily_targets(sex, weight_kg, height_cm, age, activity, goal):
    tdee = bmr_mifflin(sex, weight_kg, height_cm, age) * ACTIVITY[activity]
    if goal == "lose":
        kcal = tdee - 1000
    elif goal == "gain":
        kcal = tdee + 300
    else:
        kcal = tdee
    protein_g = 0.8 * weight_kg if goal != "gain" else 1.0 * weight_kg
    return {"kcal": round(kcal), "protein_g": round(protein_g)}
