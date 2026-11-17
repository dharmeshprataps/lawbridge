class LawbridgeError(Exception):
    """A setup, contract, execution infrastructure, or protocol error."""


class ContractError(LawbridgeError):
    pass


class ProtocolError(LawbridgeError):
    pass
