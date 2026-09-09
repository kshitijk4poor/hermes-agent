"""Compose the reported deletion dispatch hook; all transports remain ordinary."""
import runpy
import hermes_state_mutations
from hermes_state_mutation_retirement import delete_in_transaction

hermes_state_mutations._delete = delete_in_transaction
runpy.run_module('gateway.run', run_name='__main__')
