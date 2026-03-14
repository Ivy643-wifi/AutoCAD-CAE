"""AutoCAE backend — pipeline implementation organized by concern.

Structure (mirrors autosim_v2 backend layout):
    input/        — Input processing: spec loading + validation
    templates/    — Template registry, instantiator, and CAD geometry templates
    services/     — Stage execution services (CAD, mesh, solver, postprocess)
    orchestrator/ — Pipeline wiring (PipelineRunner)
"""
