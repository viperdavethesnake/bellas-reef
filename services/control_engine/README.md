# control-engine

All control loops, scheduling and interlock supervision. The **sole** publisher
of actuator commands.

Holds no hardware knowledge: it reaches devices only through the NATS subject
contract, and it never imports from `bellasreef_hardware_io`.
