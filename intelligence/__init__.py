"""FalconBridge intelligence layer (M1: evidence and egress foundation).

Application-independent: this package never imports the FOIA application (webapp, bank_workbook_generator) or any
servicing code. Each application supplies an adapter that turns its own validated, frozen outputs into evidence
records (for the FOIA application: webapp/intel_adapter.py). Nothing here calculates financial figures, calls a
network service or reads client files.
"""
