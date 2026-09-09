"""
Guardrails for validating agent outputs and ensuring deterministic quality control.
"""

from typing import Tuple, Any
import json


def security_review_output_guardrail(output: Any) -> Tuple[bool, Any]:
    """
    Validates that the Security Engineer's output adheres to strict consistency rules:
    1. Output contains valid JSON structure with 'security_vulnerabilities' and 'highest_risk'.
    2. All risk levels belong to the defined set: {'low', 'medium', 'high', 'none'}.
    3. The reported 'highest_risk' correctly reflects the maximum severity found in the vulnerabilities list.
    
    Returns:
        (True, json_dict) if valid, or (False, error_message) if validation fails.
    """
    # Extract JSON dictionary from TaskOutput object or raw output
    try:
        if isinstance(output, dict):
            json_output = output
        elif hasattr(output, "json_dict") and output.json_dict is not None:
            json_output = output.json_dict
        elif hasattr(output, "raw"):
            json_output = json.loads(output.raw)
        elif isinstance(output, str):
            json_output = json.loads(output)
        else:
            json_output = dict(output)
    except Exception as e:
        return (
            False,
            f"Error parsing task output as JSON: {str(e)}. "
            "Ensure output_json parameter is properly configured in the task."
        )

    valid_risk_levels = ["critical", "high", "medium", "low", "none"]

    # Check highest_risk field presence
    highest_risk = str(json_output.get("highest_risk", "")).lower()
    if highest_risk not in valid_risk_levels:
        return (False, f"Invalid highest risk level '{highest_risk}'. Must be one of {valid_risk_levels}.")

    # Validate vulnerabilities list
    vulnerabilities = json_output.get("security_vulnerabilities", [])
    if not isinstance(vulnerabilities, list):
        return (False, "Field 'security_vulnerabilities' must be a list.")

    for vuln in vulnerabilities:
        if not isinstance(vuln, dict):
            return (False, f"Malformed vulnerability entry: {vuln}")
        risk_level = str(vuln.get("risk_level", "")).lower()
        if risk_level not in ["critical", "high", "medium", "low"]:
            return (False, f"Invalid vulnerability risk level: '{risk_level}'. Must be 'critical', 'high', 'medium', or 'low'.")

    # Verify that highest_risk matches the maximum severity in vulnerabilities
    risk_levels = [str(vuln.get("risk_level", "")).lower() for vuln in vulnerabilities]

    if "critical" in risk_levels:
        if highest_risk != "critical":
            return (False, f"Highest risk level is set to '{highest_risk}' but critical-risk vulnerabilities exist.")
    elif "high" in risk_levels:
        if highest_risk != "high":
            return (False, f"Highest risk level is set to '{highest_risk}' but high-risk vulnerabilities exist.")
    elif "medium" in risk_levels:
        if highest_risk != "medium":
            return (False, f"Highest risk level is set to '{highest_risk}' but medium-risk vulnerabilities exist.")
    elif "low" in risk_levels:
        if highest_risk != "low":
            return (False, f"Highest risk level is set to '{highest_risk}' but low-risk vulnerabilities exist.")
    else:
        # No vulnerabilities found
        if highest_risk not in ["none", "low"]:
            return (False, f"No vulnerabilities reported, but highest_risk is '{highest_risk}'. Expected 'none' or 'low'.")

    return (True, json_output)
