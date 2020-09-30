import configparser
import csv
import datetime
import json
import logging
import os
import sys

from py_jama_rest_client.client import JamaClient, APIException

logger = logging.getLogger(__name__)


def init_logging():
    try:
        os.makedirs('logs')
    except FileExistsError:
        pass
    current_date_time = datetime.datetime.now().strftime("%Y-%m-%d %H_%M_%S")
    log_file = 'logs/dox_importer_' + str(current_date_time) + '.log'
    logging.basicConfig(filename=log_file, level=logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def parse_config():
    # allow the user to shorthand this and just look for the 'config.ini' file
    if len(sys.argv) == 1:
        current_dir = os.path.dirname(__file__)
        path_to_config = 'config.ini'
        if not os.path.isabs(path_to_config):
            path_to_config = os.path.join(current_dir, path_to_config)

    # use the config file location
    if len(sys.argv) == 2:
        current_dir = os.path.dirname(__file__)
        path_to_config = sys.argv[1]
        if not os.path.isabs(path_to_config):
            path_to_config = os.path.join(current_dir, path_to_config)

    # Parse config file.
    configuration = configparser.ConfigParser()
    try:
        with open(path_to_config, encoding="utf8", errors='ignore') as file:
            configuration.read_file(file)
    except Exception as e:
        logger.error("Unable to parse configuration file. exception: " + str(e))
        exit(1)

    return configuration


def create_jama_client(config):
    global instance_url
    url = None
    user_id = None
    user_secret = None
    oauth = None
    try:
        url = config['CLIENT_SETTINGS']['jama_connect_url']
        # Clean up the URL field
        while url.endswith('/') and url != 'https://' and url != 'http://':
            url = url[0:len(url) - 1]
        # If http or https method not specified in the url then add it now.
        if not (url.startswith('https://') or url.startswith('http://')):
            url = 'https://' + url
        oauth = config.getboolean('CLIENT_SETTINGS', 'oauth')
        user_id = config.get('CLIENT_SETTINGS', 'user_id').strip()
        user_secret = config.get('CLIENT_SETTINGS', 'user_secret').strip()
        instance_url = url
    except configparser.Error as config_error:
        logger.error("Unable to parse CLIENT_SETTINGS from config file because: {}, "
                     "Please check config file for errors and try again."
                     .format(str(config_error)))
        exit(1)

    return JamaClient(url, (user_id, user_secret), oauth=oauth)


def process_csv():
    csv_content = []
    csv_lines_read = 0

    # get the CSV script params from the config file
    csv_file = ''
    csv_using_header = True
    csv_header_value = ''
    try:
        csv_file = conf['SCRIPT_PARAMETERS']['csv_file_path']
        csv_using_header = conf.getboolean('SCRIPT_PARAMETERS', 'csv_using_header')
        if csv_using_header:
            csv_header_value = conf.get('SCRIPT_PARAMETERS', 'csv_header_value').strip()
    except Exception as e:
        logger.error(
            'unable to retrieve required script parameters csv_using_header/csv_header_value. e:{}'.format(str(e)))
        exit(1)

    # validate that we have the required script settings
    if csv_using_header and (csv_header_value is None or csv_header_value == ''):
        logger.error('missing required script param csv_header_value')
        exit(1)
    if csv_file is None or csv_file == '':
        logger.error('missing required script param csv_file_path')
        exit(1)

    # Open the CSV file for reading, use the utf-8-sig encoding to deal with excel file type outputs.
    with open(str(csv_file), encoding='utf-8-sig') as open_csv_file:
        # We need to get a dict reader, if the CSV file has headers, we dont need to supply them
        csv_dict_reader = csv.DictReader(open_csv_file)

        # validate that we have a valid username column
        if csv_using_header and csv_header_value not in csv_dict_reader.fieldnames:
            logger.error('unable to find the identifier column{} in the CSV file. script exiting...')
            exit(1)

        # Begin processing the data in the CSV file.
        for row_number, row_data in enumerate(csv_dict_reader):
            # For each row in the CSV file we will append an object to a list for later processing.
            # First get source and target data. These are mandatory, a missing data point here is an error.
            csv_lines_read += 1

            # run some quick validations that we have data in the cells
            current_row_rel_data = {
                'row': row_number + 1 if csv_using_header else row_number,
                'id': row_data[csv_header_value] if csv_using_header else json.dumps(row_data),
            }
            csv_content.append(current_row_rel_data)

    logger.info('Successfully processed {} CSV rows. from file {}'.format(str(csv_lines_read), csv_file))
    return csv_content


if __name__ == "__main__":

    #  high level script logic:
    #
    #  1. parse the csv content to get the list of unique identifiers
    #  2. fetch the corresponding Jama API ID for each unique identifier in the list
    #  3. update each item's mapped field flag to be "checked"
    #

    # Setup logging
    init_logging()

    # Get Config File Path
    conf = parse_config()

    # Create Jama Client
    jama_client = create_jama_client(conf)

    # get the delete flag field map from the config
    deleted_flag_field_map = {}
    try:
        json_string = conf.get('SCRIPT_PARAMETERS', 'deleted_flag_field_map')
        deleted_flag_field_map = json.loads(json_string)
    except Exception as e:
        logger.error('unable to parse out deleted_flag_field_map from the config.ini, e:{}'.format(str(e)))

    # process the csv content
    csv_items = process_csv()
    item_list = []
    fetch_counter = 0
    for csv_item in csv_items:
        fetch_counter += 1
        fetched_items = []
        try:
            logger.info('{}/{} processing item with identifier:{} ...'.format(fetch_counter, len(csv_items),
                                                                                         csv_item.get('id')))
            fetched_items = jama_client.get_abstract_items(contains=csv_item.get('id'))
        except APIException as e:
            logger.error(
                '    ERROR unable to retrieve items for csv entry: {} with error: {}, skipping item...'.format(str(csv_item),
                                                                                                     str(e)))
            break

        # we MUST have only one match here to continue
        if len(fetched_items) == 0:
            logger.error('    ERROR found zero matches for csv entry: {}. skipping item...'.format(str(csv_item)))
            break
        # more than one match found
        elif len(fetched_items) > 1:
            logger.error('   ERROR found multiple matches for csv entry: {}. skipping item...'.format(str(csv_item)))
            break
        # otherwise this exactly one match found
        else:
            # save this item to the item list
            item_list.append(fetched_items[0])
            logger.info('    found match to corresponding Jama ID: {}'.format(fetched_items[0].get('id')))

    # loop over the item list and check to see if we need to update the item
    # this work might already be done.
    update_list = []
    item_counter = 0
    for item in item_list:
        item_counter += 1
        # get the field from the delete field map
        item_type_id = str(item.get('itemType'))
        # validate that the mapping has the current item type
        if item_type_id not in deleted_flag_field_map:
            logger.error(
                'itemtype ID:{} is missing from the config.ini deleted_flag_field_map. skipping item...'.format(
                    item_type_id))
            break

        # format the delete field to be: fieldName $ itemTypeId (custom field)
        delete_field_name = '{}${}'.format(deleted_flag_field_map.get(item_type_id), item_type_id)

        # validate that the fields payload has the delete field
        if delete_field_name not in item.get('fields'):
            logger.error('item does not have the field: {}. skipping item...'.format(delete_field_name))
            break

        # we only need to update this item if its currently set to False.
        if not item.get('fields').get(delete_field_name):
            update_list.append((item.get('id'), delete_field_name))
        else:
            logger.info('item ID:{} already flagged for delete skipping item...'.format(item.get('id')))

    logger.info('Identified {} items to be flagged for delete'.format(len(update_list)))
    update_counter = 0

    # iterate over the update list and update each item
    for update_item in update_list:
        try:
            update_counter += 1
            patch_payload = {
                'op': 'replace',
                'path': '/fields/' + update_item[1],
                'value': True
            }
            jama_client.patch_item(update_item[0], patch_payload)
            logger.info('item {}/{} - successfully updated item id:{} to be flagged for delete'.format(update_counter,
                                                                                                       len(update_list),
                                                                                                       update_item[0]))
        except APIException as e:
            logger.error('unable to update item ID:{} exception:{}'.format(update_item[0], str(e)))

    logger.info('Script Complete')
